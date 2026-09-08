"""The public P3 line publishes its candidate before opening the matching PR."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path

import httpx
import pytest
import sqlalchemy as sa

from atelier2.adapters.candidate_store import CANDIDATE_STORE_DIRECTORY_NAME
from atelier2.adapters.dbos.advancer import (
    RunEffectConflict,
    graph_action_intent,
    prepared_effect_intent,
)
from atelier2.adapters.dbos.agent_catalog import DbosAgentConfigurationCatalog
from atelier2.adapters.dbos.catalog_store import DbosCatalogStore
from atelier2.adapters.dbos.effect_store import (
    commit_resolution,
    intent_snapshot_from_record,
    observe_adapter,
    resolve_observation,
)
from atelier2.adapters.dbos.runtime import DbosRuntime, DbosRuntimeSettings
from atelier2.adapters.dbos.schema import (
    agent_attempts,
    effect_intents,
    effect_receipts,
    run_events,
    run_inputs_v3,
)
from atelier2.adapters.dbos.starter import (
    DbosDurableRunStarter,
    DbosWorkflowRevisionPublisher,
)
from atelier2.adapters.git_transport.effects import (
    GitCommandResult,
    GitRemote,
    GitTransportEffectAdapterFactory,
    SubprocessGitCommandRunner,
)
from atelier2.adapters.github.effects import (
    GitHubEffectAdapterFactory,
    ReviewedDocumentationPublisher,
    ReviewedDocumentationPublisherFactory,
)
from atelier2.adapters.loopback import LoopbackEffectAdapterFactory
from atelier2.api.openapi import API_PREFIX
from atelier2.contracts.adapter_operations_v3 import AdapterOperationName
from atelier2.contracts.agents import (
    AgentBinding,
    AgentBindingSet,
    AgentConfigurationRevision,
    AgentConfigurationRevisionFormatVersion,
    AgentExecutionCapability,
    AgentExecutorRevision,
    AgentRole,
    AuthMode,
    AuthProfileRevision,
    ProviderId,
)
from atelier2.contracts.effect_requests import (
    OpenPullRequest,
    ReviewedDocumentationPullRequest,
    ReviewedDocumentReplacement,
    head_branch_for_queue_item,
    reviewed_documentation_candidate_digest,
)
from atelier2.contracts.effects import (
    AdapterRevision,
    EffectDestination,
    EffectIntent,
)
from atelier2.contracts.executions import (
    AgentExecutionRefusal,
    AgentNodeRefusalRecord,
    RunEventKind,
)
from atelier2.contracts.hashing import Sha256Hash
from atelier2.contracts.host_configuration import ProjectId
from atelier2.contracts.orders import InlineOrderValue, ObservedWorkItemOrderValue
from atelier2.contracts.queue_projection import TrackerItemReference, WorkItemReference
from atelier2.contracts.revisions_v3 import PublishedRevision, RevisionKind
from atelier2.contracts.runs import RunId, RunState, WorkflowRevision
from atelier2.contracts.tool_grants_v3 import ToolGrantCapability
from atelier2.contracts.when import RecordedAt
from atelier2.contracts.work_items import (
    WORK_ITEM_ORDER_SCHEMA_DOCUMENT,
    ObservedWorkItemRevision,
    WorkItemChangeMarker,
    WorkItemKind,
)
from atelier2.ports.agent_configurations import (
    AgentConfigurationRevisionCreated,
    AgentConfigurationRevisionExisting,
    AuthProfileRevisionCreated,
    AuthProfileRevisionExisting,
)
from atelier2.ports.durable_runs import (
    AuthoredOrder,
    DurableRunCreated,
    StartPublishedRunRequestV2,
    StartPublishedRunRequestV3,
)
from atelier2.ports.effects import EffectAdapterRegistration, EffectAdapterRegistry
from atelier2.ports.issue_observation import WorkItemRevisionObserved
from atelier2.ports.published_revisions import (
    PublishedRevisionCreated,
    PublishedRevisionExisting,
)
from atelier2.ports.run_queries import NodeDetailFound
from tests.integration.test_claim_checkouts import worktree_facts
from tests.scenarios.agents import (
    RecordingAgentExecutorFactoryV2,
    agent_scratch_root,
    launching,
    publish_checked_model_registry,
)
from tests.scenarios.api import durable_api_client, durable_queries
from tests.scenarios.head_branch_pull_requests import FakeHeadBranchPullRequests
from tests.scenarios.issue_observation import FakeTrackerItemSource
from tests.scenarios.run_waiting import wait_for_run_state
from tests.scenarios.work_item_claims import fake_agent_claim_executable
from tests.scenarios.workflows import ANY_JSON_SCHEMA, declared_output

PROJECT = ProjectId("p3-public")
ITEM = TrackerItemReference("gh:642")
RELEASE_ITEM = TrackerItemReference("gh:162")
RELEASE_RUN = RunId("v3/documentation-release")
RUN = RunId("v3/push-before-open-pr")
ORDER_NAME = "work-item"
AGENT_OUTPUT = b'{"summary":"publish the captured candidate"}'
_WRITE_CANDIDATE = (
    "import os,pathlib,sys;"
    "pathlib.Path('candidate.txt').write_bytes(bytes.fromhex(sys.argv[1]));"
    "os.write(1,bytes.fromhex(sys.argv[2]))"
)


def _git(repository: Path, *arguments: str) -> str:
    return _git_bytes(repository, *arguments).decode().strip()


def _git_bytes(repository: Path, *arguments: str) -> bytes:
    environment = {
        **os.environ,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_AUTHOR_NAME": "fixture",
        "GIT_AUTHOR_EMAIL": "fixture@example.test",
        "GIT_COMMITTER_NAME": "fixture",
        "GIT_COMMITTER_EMAIL": "fixture@example.test",
    }
    completed = subprocess.run(
        ("git", "-C", str(repository), *arguments),
        env=environment,
        capture_output=True,
        check=True,
    )
    return completed.stdout


def linked_worktrees(project: Path) -> tuple[Path, ...]:
    """Every worktree the project checkout registers beside itself."""

    listed = _git(project, "worktree", "list", "--porcelain")
    return tuple(
        Path(line.removeprefix("worktree "))
        for line in listed.splitlines()
        if line.startswith("worktree ") and line != f"worktree {project}"
    )


def _repositories(root: Path) -> tuple[Path, Path, str]:
    project = root / "project"
    project.mkdir()
    _git(project, "init", "--quiet", "--initial-branch=main")
    (project / "base.txt").write_text("base\n", encoding="utf-8")
    _git(project, "add", "base.txt")
    _git(project, "commit", "--quiet", "-m", "base")
    base = _git(project, "rev-parse", "HEAD")
    remote = root / "remote.git"
    _git(root, "init", "--bare", "--quiet", str(remote))
    _git(project, "push", "--quiet", str(remote), "HEAD:refs/heads/main")
    return project, remote, base


@pytest.mark.proves("a-node-binds-one-exec-shaped-and-one-effect-shaped-grant-together")
def test_a_node_binding_both_grant_shapes_together_starts_a_run(
    tmp_path: Path,
) -> None:
    """The first Done-when sentence of #1101, pinned at run level: a node pins
    `run-project-verification` (exec-shaped) and `push-atelier-commit`
    (effect-shaped) together in one workflow revision, and a run starts with
    it. The cheapest proof of that -- a start, not a launch -- next to the
    scenario that exercises `push-atelier-commit` end to end.
    """
    push_operation = PublishedRevision(
        RevisionKind.ADAPTER_OPERATION,
        json.dumps(
            {
                "operation": AdapterOperationName.PUSH_ATELIER_COMMIT.value,
                "author": {"name": "Atelier Agent", "email": "agent@example.test"},
                "committer": {"name": "Atelier Core", "email": "core@example.test"},
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode(),
    )
    push_grant = PublishedRevision(
        RevisionKind.TOOL,
        json.dumps(
            {
                "capability": ToolGrantCapability.PUSH_ATELIER_COMMIT.value,
                "operation": {
                    "ref": "push-atelier-commit",
                    "revision": push_operation.revision_hash.value,
                },
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode(),
    )
    verification_grant = PublishedRevision(
        RevisionKind.TOOL,
        json.dumps(
            {"capability": ToolGrantCapability.RUN_PROJECT_VERIFICATION.value},
            sort_keys=True,
            separators=(",", ":"),
        ).encode(),
    )
    runtime = DbosRuntime(
        DbosRuntimeSettings(
            tmp_path / "atelier.sqlite",
            "two-grant-shapes-start-test",
            agent_scratch_root=agent_scratch_root(tmp_path),
        ),
        LoopbackEffectAdapterFactory(
            tmp_path / "effects.sqlite",
            AdapterRevision("loopback-v1"),
            EffectDestination("loopback-test"),
        ),
        (RecordingAgentExecutorFactoryV2("exact", "exact/v1", "exact-operation", b""),),
    )
    runtime.initialize_storage()
    try:
        store = DbosCatalogStore(runtime.engine)
        for revision in (
            ANY_JSON_SCHEMA,
            push_operation,
            push_grant,
            verification_grant,
        ):
            published = store.publish_revision(revision)
            assert isinstance(
                published, (PublishedRevisionCreated, PublishedRevisionExisting)
            ), published

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
            f"""format_version: 3
name: One node pinning both grant shapes
nodes:
  - id: implement
    type: agent
    role: builder
    mode: headless
    instruction: Verify and push, with both grants pinned on one node.
    tools:
      - {{ref: run-project-verification, revision: {verification_grant.revision_hash.value}}}
      - {{ref: push-atelier-commit, revision: {push_grant.revision_hash.value}}}
""".encode()
            + declared_output()
        )
        workflow = WorkflowRevision(document)
        DbosWorkflowRevisionPublisher(runtime.engine).publish(workflow)
        bindings = AgentBindingSet(
            (AgentBinding(AgentRole("builder"), configuration.revision_hash),)
        )

        started = DbosDurableRunStarter(
            runtime.engine, runtime.settings, runtime.agent_executor_registry
        ).start_published(
            StartPublishedRunRequestV2(
                RunId("v3/two-grant-shapes"), workflow.revision_hash, bindings
            )
        )
    finally:
        runtime.close()

    assert isinstance(started, DurableRunCreated), started


def _publish(runtime: DbosRuntime) -> tuple[WorkflowRevision, AgentBindingSet]:
    push_operation = PublishedRevision(
        RevisionKind.ADAPTER_OPERATION,
        json.dumps(
            {
                "operation": AdapterOperationName.PUSH_ATELIER_COMMIT.value,
                "author": {
                    "name": "Atelier Agent",
                    "email": "agent@example.test",
                },
                "committer": {
                    "name": "Atelier Core",
                    "email": "core@example.test",
                },
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode(),
    )
    open_pr_operation = PublishedRevision(
        RevisionKind.ADAPTER_OPERATION,
        b'{"operation":"open-pr"}',
    )
    push_grant = PublishedRevision(
        RevisionKind.TOOL,
        json.dumps(
            {
                "capability": ToolGrantCapability.PUSH_ATELIER_COMMIT.value,
                "operation": {
                    "ref": "push-atelier-commit",
                    "revision": push_operation.revision_hash.value,
                },
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode(),
    )
    work_item_schema = PublishedRevision(
        RevisionKind.SCHEMA, WORK_ITEM_ORDER_SCHEMA_DOCUMENT
    )
    store = DbosCatalogStore(runtime.engine)
    for revision in (
        ANY_JSON_SCHEMA,
        work_item_schema,
        push_operation,
        open_pr_operation,
        push_grant,
    ):
        published = store.publish_revision(revision)
        assert isinstance(
            published, (PublishedRevisionCreated, PublishedRevisionExisting)
        ), published

    catalog = DbosAgentConfigurationCatalog(
        runtime.engine, runtime.agent_executor_registry
    )
    auth = AuthProfileRevision("max", 1, ProviderId("exact"), AuthMode.SUBSCRIPTION)
    assert isinstance(
        catalog.publish_auth_profile_revision(auth),
        (AuthProfileRevisionCreated, AuthProfileRevisionExisting),
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
        (AgentConfigurationRevisionCreated, AgentConfigurationRevisionExisting),
    )
    publish_checked_model_registry(
        runtime.engine, ProviderId("exact"), (configuration,)
    )

    document = (
        f"""format_version: 3
name: Push before opening the pull request
graph_inputs:
  - name: {ORDER_NAME}
    schema:
      ref: work-item
      revision: {work_item_schema.revision_hash.value}
nodes:
  - id: implement
    type: agent
    role: builder
    mode: headless
    instruction: Implement the named work item.
    tools:
      - {{ref: push-atelier-commit, revision: {push_grant.revision_hash.value}}}
    inputs:
      - name: {ORDER_NAME}
        from:
          graph_input: {ORDER_NAME}
""".encode()
        + declared_output()
        + f"""  - id: publish
    type: action
    operation: {{ref: open-pr, revision: {open_pr_operation.revision_hash.value}}}
    depends_on: [implement]
    inputs:
      - name: body
        from: {{node: implement, output: result}}
""".encode()
    )
    workflow = WorkflowRevision(document)
    DbosWorkflowRevisionPublisher(runtime.engine).publish(workflow)
    return workflow, AgentBindingSet(
        (AgentBinding(AgentRole("builder"), configuration.revision_hash),)
    )


def _public_runtime(
    tmp_path: Path,
    project: Path,
    remote: Path,
    *,
    claims: str | None = "checkout",
    claim_root: Path | None = None,
) -> tuple[DbosRuntime, GitHubEffectAdapterFactory]:
    """The served instance this proof drives: both effects, and its claim ledger.

    `claims` is what that ledger answers a claim -- by default only what a
    clean linked worktree on the lane branch earns, as the real tool answers;
    or `None` for an operator who served the project without a claim command
    at all, which is what the unconfigured refusal is about.
    """

    github = GitHubEffectAdapterFactory(
        tmp_path / "github.sqlite",
        AdapterRevision("github-open-pr-v1"),
        EffectDestination("platform"),
    )
    push = GitTransportEffectAdapterFactory(
        tmp_path / CANDIDATE_STORE_DIRECTORY_NAME,
        GitRemote("local-public-test", str(remote)),
        AdapterRevision("git-push-v1"),
        EffectDestination("git"),
        FakeHeadBranchPullRequests(),
    )
    registry = EffectAdapterRegistry(
        (
            EffectAdapterRegistration(AdapterOperationName.OPEN_PR, github),
            EffectAdapterRegistration(AdapterOperationName.PUSH_ATELIER_COMMIT, push),
        )
    )
    executor = RecordingAgentExecutorFactoryV2(
        "exact",
        "exact/v1",
        "exact-operation",
        AGENT_OUTPUT,
        command=launching(
            sys.executable,
            "-c",
            _WRITE_CANDIDATE,
            b"candidate exact bytes\n".hex(),
            AGENT_OUTPUT.hex(),
        ),
    )
    runtime = DbosRuntime(
        DbosRuntimeSettings(
            tmp_path / "atelier.sqlite",
            "p3-public-test",
            agent_scratch_root=agent_scratch_root(tmp_path),
            project_id=PROJECT,
            bootstrap_project_root=project,
            aco_executable=(
                None
                if claims is None
                else fake_agent_claim_executable(claim_root or tmp_path, claims)
            ),
        ),
        registry,
        (executor,),
    )
    runtime.initialize_storage()
    return runtime, github


def _start_public_run(
    runtime: DbosRuntime, body: bytes, run_id: RunId = RUN
) -> httpx.Response:
    """Start the shipped line on one issue whose body says exactly this."""

    workflow, bindings = _publish(runtime)
    item = ObservedWorkItemRevision(
        ITEM,
        WorkItemKind.ISSUE,
        body,
        WorkItemChangeMarker("issue-642-v1"),
        RecordedAt("2026-08-27T12:00:00Z"),
    )
    binding = bindings.bindings[0]
    client = durable_api_client(
        runtime,
        served_project_id=PROJECT,
        tracker_item_source=FakeTrackerItemSource(
            snapshot_answer=WorkItemRevisionObserved(item),
            expected_snapshot_reference=item.item,
        ),
    )
    return client.post(
        API_PREFIX + "/runs",
        json={
            "workflow_format_version": 3,
            "run_id": run_id.value,
            "workflow_revision_hash": workflow.revision_hash.value,
            "agent_bindings": [
                {
                    "role": binding.role.value,
                    "agent_configuration_revision_hash": (
                        binding.agent_configuration_revision_hash.value
                    ),
                }
            ],
            "orders": [{"name": ORDER_NAME, "work_item": ITEM.value}],
        },
    )


_SCOPED_ITEM = b"Implement P3.\n\n## Dateien\n`one.txt`\n"
_LANE_BRANCH = head_branch_for_queue_item(WorkItemReference(PROJECT, ITEM).item_id)


@pytest.mark.parametrize(
    ("body", "claims", "refusal", "receipts"),
    (
        pytest.param(
            b"Implement P3.",
            "grant",
            AgentNodeRefusalRecord(AgentExecutionRefusal.WORK_ITEM_NAMES_NO_SCOPE),
            0,
            id="the-item-names-no-scope",
        ),
        pytest.param(
            _SCOPED_ITEM,
            None,
            AgentNodeRefusalRecord(AgentExecutionRefusal.WORK_ITEM_CLAIM_UNCONFIGURED),
            0,
            id="this-instance-holds-no-claim-command",
        ),
        pytest.param(
            _SCOPED_ITEM,
            "priority",
            AgentNodeRefusalRecord(
                AgentExecutionRefusal.WORK_ITEM_CLAIM_REFUSED_BY_PRIORITY,
                "a higher-priority item is free",
            ),
            0,
            id="the-ledger-refuses-on-priority",
        ),
        pytest.param(
            _SCOPED_ITEM,
            "unknown",
            AgentNodeRefusalRecord(AgentExecutionRefusal.WORK_ITEM_CLAIM_REFUSED),
            0,
            id="the-ledger-refuses-without-a-reason-this-reader-knows",
        ),
        pytest.param(
            _SCOPED_ITEM,
            "checkout-refused",
            AgentNodeRefusalRecord(
                AgentExecutionRefusal.WORK_ITEM_CLAIM_REFUSED,
                f"claim branch '{_LANE_BRANCH.value}' does not match checkout "
                "branch 'main'",
            ),
            0,
            id="the-tool-refuses-the-checkout-before-any-json",
        ),
        pytest.param(
            _SCOPED_ITEM,
            "touches",
            AgentNodeRefusalRecord(
                AgentExecutionRefusal.WORK_ITEM_CLAIM_TOUCHES_ANOTHER_LANE,
                "item 77 on one.txt",
            ),
            1,
            id="the-claim-touches-another-lane",
        ),
    ),
)
def test_a_claim_this_run_cannot_hold_ends_the_node_before_any_work(
    tmp_path: Path,
    body: bytes,
    claims: str | None,
    refusal: AgentNodeRefusalRecord,
    receipts: int,
) -> None:
    """A claim this run cannot hold ends it where it stands, and nothing ran.

    The scope is the item's own `## Dateien` and a wide claim is not this
    runtime's to invent; an instance serving without a claim command holds no
    lane at all; and the ledger itself may refuse, or grant a claim over paths
    another lane already holds. Every one of them ends the node under its own
    word before an attempt of it exists -- which is what proves no workspace
    was leased, no provider started and nothing was pushed. A grant that did
    happen is receipted all the same. Where the ledger said why, the run's
    record and the node's own detail carry that sentence verbatim.
    """

    project, remote, _base = _repositories(tmp_path)
    runtime, github = _public_runtime(tmp_path, project, remote, claims=claims)
    try:
        assert _start_public_run(runtime, body).status_code == 201
        runtime.launch()
        wait_for_run_state(runtime.engine, RUN, RunState.FAILED)

        with runtime.engine.connect() as connection:
            failures = connection.execute(
                sa.select(run_events.c.payload, run_events.c.agent_attempt_id).where(
                    run_events.c.event_kind == RunEventKind.AGENT_FAILED.value
                )
            ).all()
            attempts = connection.execute(
                sa.select(sa.func.count()).select_from(agent_attempts)
            ).scalar()
            confirmed = connection.execute(
                sa.select(sa.func.count()).select_from(effect_receipts)
            ).scalar()
        assert [
            (AgentNodeRefusalRecord.decode(bytes(payload)), attempt)
            for payload, attempt in failures
        ] == [(refusal, None)]
        assert (attempts, confirmed) == (0, receipts)
        node = durable_queries(runtime.engine).get_node_detail(RUN, "implement")
        assert isinstance(node, NodeDetailFound)
        assert node.detail.refusal == refusal.sentence()
        assert github.recorded_pull_requests() == ()
        assert _git(remote, "branch", "--list", "atelier2/*") == ""
        assert linked_worktrees(project) == ()
    finally:
        runtime.close()

    _restarts_without_an_open_binding(tmp_path, project, remote)


def _restarts_without_an_open_binding(
    tmp_path: Path, project: Path, remote: Path
) -> None:
    """A finished run leaves no effect intent a differing identity must answer for.

    The claim's binding names the command this instance was served with, so an
    instance serving the same project with another one is refused at start
    while any claim intent of a finished run still counts as open (#1218).
    """

    moved = tmp_path / "moved-claim-command"
    moved.mkdir(exist_ok=True)
    restarted, _github = _public_runtime(tmp_path, project, remote, claim_root=moved)
    restarted.close()


@pytest.mark.proves("an-authorised-candidate-is-pushed-before-its-pr-opens")
def test_public_start_pushes_the_candidate_before_opening_its_pull_request(
    tmp_path: Path,
) -> None:
    project, remote, base = _repositories(tmp_path)
    runtime, github = _public_runtime(tmp_path, project, remote)
    try:
        response = _start_public_run(runtime, _SCOPED_ITEM)
        assert response.status_code == 201, response.text
        runtime.launch()
        wait_for_run_state(runtime.engine, RUN, RunState.COMPLETED)

        branch = head_branch_for_queue_item(
            WorkItemReference(PROJECT, ITEM).item_id
        ).value
        commit = _git(remote, "rev-parse", f"refs/heads/{branch}")
        pushed_tree = _git(remote, "rev-parse", f"{commit}^{{tree}}")
        assert _git(remote, "rev-parse", f"{commit}^") == base
        assert _git(remote, "show", f"{commit}:candidate.txt") == (
            "candidate exact bytes"
        )

        recorded_pull_requests = github.recorded_pull_requests()
        assert len(recorded_pull_requests) == 1
        pull_request = recorded_pull_requests[0]
        assert pull_request.branch == branch

        with runtime.engine.connect() as connection:
            intent_rows = connection.execute(
                sa.select(effect_intents).order_by(sa.literal_column("rowid"))
            ).mappings()
            intents = tuple(
                intent_snapshot_from_record(row).intent for row in intent_rows
            )
            receipts = connection.execute(
                sa.select(
                    effect_receipts.c.operation_name,
                    effect_receipts.c.result,
                ).order_by(sa.literal_column("rowid"))
            ).all()
        # The lane claim is the run's first effect: it is held before the
        # builder works, and its receipt stands beside the push and the PR.
        assert [intent.binding.operation_name for intent in intents] == [
            AdapterOperationName.CLAIM_WORK_ITEM,
            AdapterOperationName.PUSH_ATELIER_COMMIT,
            AdapterOperationName.OPEN_PR,
        ]
        assert [row.operation_name for row in receipts] == [
            AdapterOperationName.CLAIM_WORK_ITEM.value,
            AdapterOperationName.PUSH_ATELIER_COMMIT.value,
            AdapterOperationName.OPEN_PR.value,
        ]
        push_receipt = json.loads(bytes(receipts[1].result).decode("utf-8"))
        assert push_receipt["commit_oid"] == commit
        assert push_receipt["candidate_tree"] == pushed_tree
        open_request = OpenPullRequest.from_canonical_bytes(intents[2].request.payload)
        assert open_request.head_branch.value == branch
        # The claim was held from the run's own checkout, a clean linked
        # worktree on the lane branch at the base, which the provider never
        # worked in: its lease went with the attempt, the checkout stands
        # until the claim is released.
        (checkout,) = linked_worktrees(project)
        assert checkout.is_relative_to(tmp_path / "claim-checkouts")
        facts = worktree_facts(checkout)
        assert facts["git_dir"] != facts["common_dir"]
        assert (facts["branch"], facts["head"], facts["status"]) == (branch, base, "")
        assert not (checkout / "candidate.txt").exists()
        assert not any(agent_scratch_root(tmp_path).iterdir())
    finally:
        runtime.close()

    _restarts_without_an_open_binding(tmp_path, project, remote)


class _CrashAfterDocumentationPush(RuntimeError):
    pass


@dataclass
class _CrashAfterPushPublisher:
    delegate: ReviewedDocumentationPublisher

    def publish(
        self, intent: EffectIntent, request: ReviewedDocumentationPullRequest
    ) -> None:
        self.delegate.publish(intent, request)
        raise _CrashAfterDocumentationPush

    def close(self) -> None:
        self.delegate.close()


@dataclass(frozen=True)
class _CrashAfterPushPublisherFactory:
    delegate: ReviewedDocumentationPublisherFactory

    def open(self) -> ReviewedDocumentationPublisher:
        return _CrashAfterPushPublisher(self.delegate.open())


@dataclass
class _CountingGitRunner:
    delegate: SubprocessGitCommandRunner = field(
        default_factory=SubprocessGitCommandRunner
    )
    push_calls: int = 0

    def run(
        self,
        arguments: tuple[str, ...],
        *,
        working_directory: Path,
        environment: Mapping[str, str],
        standard_input: bytes | None = None,
    ) -> GitCommandResult:
        if "push" in arguments:
            self.push_calls += 1
        return self.delegate.run(
            arguments,
            working_directory=working_directory,
            environment=environment,
            standard_input=standard_input,
        )


def test_release_resolve_after_a_push_crash_opens_one_pr_without_another_push(
    tmp_path: Path,
) -> None:
    project, remote, base = _repositories(tmp_path)
    runner = _CountingGitRunner()
    publisher = GitTransportEffectAdapterFactory(
        tmp_path / "documentation-candidates.git",
        GitRemote("documentation-release", str(remote)),
        AdapterRevision("git-push-v1"),
        EffectDestination("git"),
        FakeHeadBranchPullRequests(),
        runner,
    )
    github = GitHubEffectAdapterFactory(
        tmp_path / "github.sqlite",
        AdapterRevision("github-open-pr-v1"),
        EffectDestination("platform"),
        publisher,
    )
    runtime = DbosRuntime(
        DbosRuntimeSettings(
            tmp_path / "atelier.sqlite",
            "documentation-release-resolve-test",
            agent_scratch_root=agent_scratch_root(tmp_path),
            project_id=PROJECT,
            bootstrap_project_root=project,
        ),
        github,
    )
    runtime.initialize_storage()
    try:
        workflow = _publish_documentation_release(runtime)
        orders, replacement = _documentation_release_orders(
            base_revision=base, verdict="approve", mutate_after_digest=False
        )
        started = DbosDurableRunStarter(
            runtime.engine, runtime.settings, runtime.agent_executor_registry
        ).start_published(
            StartPublishedRunRequestV3(
                RELEASE_RUN,
                workflow.revision_hash,
                AgentBindingSet(()),
                orders=orders,
            )
        )
        assert isinstance(started, DurableRunCreated), started
        with runtime.engine.begin() as connection:
            snapshot = prepared_effect_intent(
                connection,
                graph_action_intent(
                    connection,
                    RELEASE_RUN,
                    workflow.revision_hash,
                    github.binding,
                    PROJECT,
                ),
            )
        logical_key = snapshot.intent.binding.logical_key.value
        crashing_factory = replace(
            github,
            documentation_publisher_factory=_CrashAfterPushPublisherFactory(publisher),
        )
        crashing_adapter = crashing_factory.open()
        try:
            with runtime.engine.begin() as connection:
                observed = observe_adapter(
                    connection,
                    crashing_adapter,
                    logical_key,
                    workflow.revision_hash.value,
                )
            with (
                pytest.raises(_CrashAfterDocumentationPush),
                runtime.engine.begin() as connection,
            ):
                resolve_observation(
                    connection,
                    crashing_adapter,
                    logical_key,
                    workflow.revision_hash.value,
                    observed,
                )
        finally:
            crashing_adapter.close()
        assert runner.push_calls == 1
        branch = ReviewedDocumentationPullRequest.from_canonical_bytes(
            snapshot.intent.request.payload
        ).head_branch
        pushed_commit = _git(remote, "rev-parse", branch.full_ref)
        assert _git_bytes(remote, "show", f"{pushed_commit}:base.txt") == replacement
        assert github.recorded_pull_requests() == ()

        adapter = github.open()
        try:
            with runtime.engine.begin() as connection:
                resolved = resolve_observation(
                    connection,
                    adapter,
                    logical_key,
                    workflow.revision_hash.value,
                    observed,
                )
                assert (
                    commit_resolution(
                        connection,
                        logical_key,
                        workflow.revision_hash.value,
                        resolved,
                    )
                    is RunState.STARTED
                )
        finally:
            adapter.close()
        assert runner.push_calls == 1
        assert len(github.recorded_pull_requests()) == 1
        with runtime.engine.connect() as connection:
            assert (
                connection.scalar(
                    sa.select(sa.func.count()).select_from(effect_receipts)
                )
                == 1
            )
    finally:
        runtime.close()


def _publish_documentation_release(runtime: DbosRuntime) -> WorkflowRevision:
    store = DbosCatalogStore(runtime.engine)
    revisions = (
        PublishedRevision(RevisionKind.SCHEMA, WORK_ITEM_ORDER_SCHEMA_DOCUMENT),
        PublishedRevision(
            RevisionKind.SCHEMA,
            Path("workflows/schemas/documentation_release_candidate.json").read_bytes(),
        ),
        PublishedRevision(
            RevisionKind.SCHEMA,
            Path("workflows/schemas/documentation_release_verdict.json").read_bytes(),
        ),
        PublishedRevision(RevisionKind.ADAPTER_OPERATION, b'{"operation":"open-pr"}'),
    )
    for revision in revisions:
        published = store.publish_revision(revision)
        assert isinstance(
            published, (PublishedRevisionCreated, PublishedRevisionExisting)
        ), published
    workflow = WorkflowRevision(
        Path("workflows/documentation-release.yaml").read_bytes()
    )
    DbosWorkflowRevisionPublisher(runtime.engine).publish(workflow)
    return workflow


def _documentation_release_orders(
    *, base_revision: str, verdict: str, mutate_after_digest: bool
) -> tuple[tuple[AuthoredOrder, ...], bytes]:
    replacements = (
        ReviewedDocumentReplacement(
            "base.txt", Sha256Hash.of(b"base\n").value, b"reviewed replacement\n"
        ),
    )
    title = "Reviewed documentation release"
    body = "The independently approved documentation candidate."
    candidate_digest = reviewed_documentation_candidate_digest(
        base_revision, replacements, title, body
    )
    replacement_content = (
        "unreviewed replacement\n"
        if mutate_after_digest
        else replacements[0].replacement.decode()
    )
    candidate = {
        "candidate_digest": candidate_digest,
        "base_revision": base_revision,
        "changes": [
            {
                "path": replacements[0].path,
                "current_digest": replacements[0].current_digest,
                "replacement_utf8_content": replacement_content,
            }
        ],
        "title": title,
        "body": body,
    }
    verdict_candidate_digest = (
        "f" * 64 if verdict == "digest-mismatch" else candidate_digest
    )
    verdict_document = {
        "candidate_digest": verdict_candidate_digest,
        "verdict_digest": "d" * 64,
        "verdict": (
            verdict if verdict in {"approve", "revise", "cannot-judge"} else "approve"
        ),
    }
    item = ObservedWorkItemRevision(
        RELEASE_ITEM,
        WorkItemKind.ISSUE,
        b"Release the reviewed documentation candidate.",
        WorkItemChangeMarker("issue-162-release-v1"),
        RecordedAt("2026-08-27T12:00:00Z"),
    )
    return (
        (
            AuthoredOrder("work_item", ObservedWorkItemOrderValue(item)),
            AuthoredOrder(
                "candidate", InlineOrderValue(json.dumps(candidate).encode())
            ),
            AuthoredOrder(
                "approved_verdict",
                InlineOrderValue(json.dumps(verdict_document).encode()),
            ),
        ),
        replacements[0].replacement,
    )


@pytest.mark.proves(
    "an-explicitly-released-approved-documentation-candidate-opens-one-draft-pr"
)
@pytest.mark.parametrize(
    ("verdict", "mutate_after_digest", "approved"),
    [
        pytest.param("approve", False, True, id="approved-exact-candidate"),
        pytest.param("revise", False, False, id="non-approve-verdict"),
        pytest.param("digest-mismatch", False, False, id="verdict-digest-mismatch"),
        pytest.param("approve", True, False, id="copied-digest-on-different-tree"),
        pytest.param(
            "missing-candidate-digest",
            False,
            False,
            id="malformed-candidate-is-a-typed-action-refusal",
        ),
        pytest.param(
            "missing-candidate-order",
            False,
            False,
            id="missing-declared-order-is-a-typed-action-refusal",
        ),
    ],
)
def test_the_real_release_entry_binds_approval_to_the_exact_candidate_before_effects(
    tmp_path: Path,
    verdict: str,
    mutate_after_digest: bool,
    approved: bool,
) -> None:
    project, remote, base = _repositories(tmp_path)
    runner = _CountingGitRunner()
    publisher = GitTransportEffectAdapterFactory(
        tmp_path / "documentation-candidates.git",
        GitRemote("documentation-release", str(remote)),
        AdapterRevision("git-push-v1"),
        EffectDestination("git"),
        FakeHeadBranchPullRequests(),
        runner,
    )
    github = GitHubEffectAdapterFactory(
        tmp_path / "github.sqlite",
        AdapterRevision("github-open-pr-v1"),
        EffectDestination("platform"),
        publisher,
    )
    runtime = DbosRuntime(
        DbosRuntimeSettings(
            tmp_path / "atelier.sqlite",
            "documentation-release-test",
            agent_scratch_root=agent_scratch_root(tmp_path),
            project_id=PROJECT,
            bootstrap_project_root=project,
        ),
        github,
    )
    runtime.initialize_storage()
    try:
        workflow = _publish_documentation_release(runtime)
        orders, expected_replacement = _documentation_release_orders(
            base_revision=base,
            verdict=verdict,
            mutate_after_digest=mutate_after_digest,
        )
        started = DbosDurableRunStarter(
            runtime.engine, runtime.settings, runtime.agent_executor_registry
        ).start_published(
            StartPublishedRunRequestV3(
                RELEASE_RUN,
                workflow.revision_hash,
                AgentBindingSet(()),
                orders=orders,
            )
        )
        assert isinstance(started, DurableRunCreated), started

        if verdict == "missing-candidate-digest":
            with runtime.engine.begin() as connection:
                connection.exec_driver_sql("DROP TRIGGER run_inputs_v3_no_update")
                stored_candidate = connection.scalar(
                    sa.select(run_inputs_v3.c.value).where(
                        run_inputs_v3.c.run_id == RELEASE_RUN.value,
                        run_inputs_v3.c.name == "candidate",
                    )
                )
                assert stored_candidate is not None
                candidate_bytes = bytes(stored_candidate)
                malformed_candidate = json.loads(candidate_bytes)
                del malformed_candidate["candidate_digest"]
                malformed_bytes = json.dumps(malformed_candidate).encode()
                connection.execute(
                    run_inputs_v3.update()
                    .where(
                        run_inputs_v3.c.run_id == RELEASE_RUN.value,
                        run_inputs_v3.c.name == "candidate",
                    )
                    .values(
                        value=malformed_bytes,
                        value_hash=Sha256Hash.of(malformed_bytes).value,
                    )
                )
        if verdict == "missing-candidate-order":
            with runtime.engine.begin() as connection:
                connection.exec_driver_sql("DROP TRIGGER run_inputs_v3_no_delete")
                connection.execute(
                    run_inputs_v3.delete().where(
                        run_inputs_v3.c.run_id == RELEASE_RUN.value,
                        run_inputs_v3.c.name == "candidate",
                    )
                )

        if not approved:
            with runtime.engine.begin() as connection:
                with pytest.raises(RunEffectConflict):
                    graph_action_intent(
                        connection,
                        RELEASE_RUN,
                        workflow.revision_hash,
                        github.binding,
                        PROJECT,
                    )
                assert (
                    connection.scalar(
                        sa.select(sa.func.count()).select_from(effect_intents)
                    )
                    == 0
                )
            assert runner.push_calls == 0
            assert github.recorded_pull_requests() == ()
            return

        runtime.launch()
        wait_for_run_state(runtime.engine, RELEASE_RUN, RunState.COMPLETED)
        pull_requests = github.recorded_pull_requests()
        assert runner.push_calls == 1
        assert len(pull_requests) == 1
        with runtime.engine.connect() as connection:
            intent = intent_snapshot_from_record(
                connection.execute(sa.select(effect_intents)).mappings().one()
            ).intent
            assert (
                connection.scalar(
                    sa.select(sa.func.count()).select_from(effect_receipts)
                )
                == 1
            )
        request = ReviewedDocumentationPullRequest.from_canonical_bytes(
            intent.request.payload
        )
        assert request.reviewed_verdict_digest
        assert request.draft is True
        assert request.replacements[0].replacement == expected_replacement
        pushed_commit = _git(remote, "rev-parse", request.head_branch.full_ref)
        assert _git(remote, "rev-parse", f"{pushed_commit}^") == request.base_revision
        assert _git_bytes(remote, "show", f"{pushed_commit}:base.txt") == (
            request.replacements[0].replacement
        )
        assert pull_requests[0].branch == request.head_branch.value
    finally:
        runtime.close()
