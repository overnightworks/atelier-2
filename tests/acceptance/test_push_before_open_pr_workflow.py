"""The repository workflow publishes its candidate before opening its pull request."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
import sqlalchemy as sa

from atelier2.adapters.candidate_store import CANDIDATE_STORE_DIRECTORY_NAME
from atelier2.adapters.dbos.advancer import RunEffectConflict, graph_action_intent
from atelier2.adapters.dbos.agent_catalog import DbosAgentConfigurationCatalog
from atelier2.adapters.dbos.catalog_store import DbosCatalogStore
from atelier2.adapters.dbos.effect_store import intent_snapshot_from_record
from atelier2.adapters.dbos.runtime import DbosRuntime, DbosRuntimeSettings
from atelier2.adapters.dbos.schema import effect_intents, effect_receipts, runs
from atelier2.adapters.dbos.starter import DbosWorkflowRevisionPublisher
from atelier2.adapters.git_transport.effects import (
    GitRemote,
    GitTransportEffectAdapterFactory,
)
from atelier2.adapters.github.effects import GitHubEffectAdapterFactory
from atelier2.adapters.yaml_workflows import parse_workflow_document
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
from atelier2.contracts.budgets_v3 import (
    BudgetRevisionAccepted,
    read_budget_revision_document,
)
from atelier2.contracts.effect_requests import (
    GitCommitIdentity,
    OpenPullRequest,
    PushAtelierCommitReceipt,
)
from atelier2.contracts.effects import (
    AdapterRevision,
    EffectDestination,
)
from atelier2.contracts.host_configuration import ProjectId
from atelier2.contracts.queue_projection import TrackerItemReference
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
from atelier2.contracts.workflows_v3 import AgentNodeV3
from atelier2.ports.agent_configurations import (
    AgentConfigurationRevisionCreated,
    AuthProfileRevisionCreated,
)
from atelier2.ports.effects import EffectAdapterRegistration, EffectAdapterRegistry
from atelier2.ports.issue_observation import WorkItemRevisionObserved
from atelier2.ports.published_revisions import (
    PublishedRevisionCreated,
    PublishedRevisionExisting,
)
from tests.scenarios.agents import (
    RecordingAgentExecutorFactoryV2,
    agent_scratch_root,
    launching,
    publish_checked_model_registry,
)
from tests.scenarios.api import durable_api_client
from tests.scenarios.head_branch_pull_requests import FakeHeadBranchPullRequests
from tests.scenarios.issue_observation import FakeTrackerItemSource
from tests.scenarios.projects import run_git
from tests.scenarios.run_waiting import wait_for_run_state
from tests.scenarios.work_item_claims import fake_agent_claim_executable

WORKFLOW_PATH = Path("workflows/push-before-open-pr.yaml")
BUDGET_PATH = Path("workflows/budgets/push-implement.json")
PROJECT = ProjectId("push-before-open-pr-workflow")
ITEM = TrackerItemReference("gh:883")
RUN = RunId("v3/repository-push-before-open-pr")
AGENT_OUTPUT = b'"publish this candidate"'
_WRITE_CANDIDATE = (
    "import os,pathlib,sys;"
    "pathlib.Path('candidate.txt').write_bytes(bytes.fromhex(sys.argv[1]));"
    "os.write(1,bytes.fromhex(sys.argv[2]))"
)


def _repositories(root: Path) -> tuple[Path, Path, str]:
    project = root / "project"
    project.mkdir()
    run_git(project, "init", "--quiet", "--initial-branch=main")
    (project / "base.txt").write_text("base\n", encoding="utf-8")
    run_git(project, "add", "base.txt")
    run_git(project, "commit", "--quiet", "-m", "base")
    base = run_git(project, "rev-parse", "HEAD")
    remote = root / "remote.git"
    run_git(root, "init", "--bare", "--quiet", str(remote))
    run_git(project, "push", "--quiet", str(remote), "HEAD:refs/heads/main")
    return project, remote, base


def _publish_workflow(
    runtime: DbosRuntime,
) -> tuple[
    WorkflowRevision,
    AgentBindingSet,
    tuple[GitCommitIdentity, GitCommitIdentity],
]:
    # The published live revisions pin the operator as author and the pushing
    # node's model as committer (issue #883, operator ruling 30.08.2026); the
    # shipped document binds difficulty 2, which the project defaults answer
    # with grok-4.6. Reproducing that exact pair is what makes the derived
    # grant hash equal the one the shipped document pins.
    connected_account_address = "44832414+FlexOr2@users.noreply.github.com"
    author = GitCommitIdentity("Felix Hummert", connected_account_address)
    pushing_model = "grok-4.6"
    committer = GitCommitIdentity("Grok 4.6", connected_account_address)
    push_operation = PublishedRevision(
        RevisionKind.ADAPTER_OPERATION,
        json.dumps(
            {
                "operation": AdapterOperationName.PUSH_ATELIER_COMMIT.value,
                "author": author.as_json(),
                "committer": committer.as_json(),
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
    budget = PublishedRevision(RevisionKind.BUDGET_POLICY, BUDGET_PATH.read_bytes())
    revisions = (
        PublishedRevision(
            RevisionKind.SCHEMA,
            Path("workflows/schemas/nonempty_string.json").read_bytes(),
        ),
        budget,
        PublishedRevision(RevisionKind.SCHEMA, WORK_ITEM_ORDER_SCHEMA_DOCUMENT),
        push_operation,
        PublishedRevision(RevisionKind.ADAPTER_OPERATION, b'{"operation":"open-pr"}'),
        push_grant,
    )
    store = DbosCatalogStore(runtime.engine)
    for revision in revisions:
        published = store.publish_revision(revision)
        assert isinstance(
            published, (PublishedRevisionCreated, PublishedRevisionExisting)
        ), published

    shipped_document = WORKFLOW_PATH.read_bytes()
    assert push_grant.revision_hash.value.encode() in shipped_document
    assert budget.revision_hash.value.encode() in shipped_document
    (pushing_node,) = (
        node
        for node in parse_workflow_document(shipped_document).nodes
        if isinstance(node, AgentNodeV3)
    )
    assert pushing_node.model == pushing_model
    workflow = WorkflowRevision(shipped_document)
    DbosWorkflowRevisionPublisher(runtime.engine).publish(workflow)

    catalog = DbosAgentConfigurationCatalog(
        runtime.engine, runtime.agent_executor_registry
    )
    auth = AuthProfileRevision(
        "workflow-test", 1, ProviderId("exact"), AuthMode.SUBSCRIPTION
    )
    assert isinstance(
        catalog.publish_auth_profile_revision(auth), AuthProfileRevisionCreated
    )
    configuration = AgentConfigurationRevision(
        "builder",
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
    bindings = AgentBindingSet(
        (AgentBinding(AgentRole("builder"), configuration.revision_hash),)
    )
    return workflow, bindings, (author, committer)


def _pinned_turn_bound() -> int:
    budget = read_budget_revision_document(BUDGET_PATH.read_bytes())
    assert isinstance(budget, BudgetRevisionAccepted)
    turns = budget.content.maximum_assistant_turns
    assert turns is not None
    return turns


@pytest.mark.proves("an-authorised-candidate-is-pushed-before-its-pr-opens")
def test_repository_workflow_binds_open_pr_to_its_confirmed_push_receipt(
    tmp_path: Path,
) -> None:
    project, remote, base = _repositories(tmp_path)
    github = GitHubEffectAdapterFactory(
        tmp_path / "github.sqlite",
        AdapterRevision("github-open-pr-v1"),
        EffectDestination("platform"),
    )
    push = GitTransportEffectAdapterFactory(
        tmp_path / CANDIDATE_STORE_DIRECTORY_NAME,
        GitRemote("local-workflow-test", str(remote)),
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
        capability_set=frozenset({AgentExecutionCapability.HEADLESS_WITH_TOOLS}),
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
            "repository-workflow-test",
            agent_scratch_root=agent_scratch_root(tmp_path),
            project_id=PROJECT,
            bootstrap_project_root=project,
            aco_executable=fake_agent_claim_executable(tmp_path),
        ),
        registry,
        (executor,),
    )
    runtime.initialize_storage()
    try:
        workflow, bindings, (author, committer) = _publish_workflow(runtime)
        item = ObservedWorkItemRevision(
            ITEM,
            WorkItemKind.ISSUE,
            b"Implement the repository workflow proof.\n\n## Bereich\none.txt\n",
            WorkItemChangeMarker("issue-883-v1"),
            RecordedAt("2026-08-29T12:00:00Z"),
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
        response = client.post(
            API_PREFIX + "/runs",
            json={
                "workflow_format_version": 3,
                "run_id": RUN.value,
                "workflow_revision_hash": workflow.revision_hash.value,
                "agent_bindings": [
                    {
                        "role": binding.role.value,
                        "agent_configuration_revision_hash": (
                            binding.agent_configuration_revision_hash.value
                        ),
                    }
                ],
                "orders": [{"name": "work_item", "work_item": ITEM.value}],
            },
        )
        assert response.status_code == 201, response.text
        runtime.launch()
        wait_for_run_state(runtime.engine, RUN, RunState.COMPLETED)

        assert executor.opened is not None
        (implement_request,) = executor.opened.requests
        assert implement_request.maximum_assistant_turns == _pinned_turn_bound()

        with runtime.engine.connect() as connection:
            final_intents = tuple(
                intent_snapshot_from_record(row).intent
                for row in connection.execute(
                    sa.select(effect_intents).order_by(sa.literal_column("rowid"))
                ).mappings()
            )
            receipts = connection.execute(
                sa.select(
                    effect_receipts.c.operation_name,
                    effect_receipts.c.result,
                ).order_by(sa.literal_column("rowid"))
            ).all()
        # The claim stands first: the run held its item's lane before the
        # builder ran, and its receipt is in the same ledger as the push.
        assert [intent.binding.operation_name for intent in final_intents] == [
            AdapterOperationName.CLAIM_WORK_ITEM,
            AdapterOperationName.PUSH_ATELIER_COMMIT,
            AdapterOperationName.OPEN_PR,
        ]
        assert [receipt.operation_name for receipt in receipts] == [
            AdapterOperationName.CLAIM_WORK_ITEM.value,
            AdapterOperationName.PUSH_ATELIER_COMMIT.value,
            AdapterOperationName.OPEN_PR.value,
        ]
        receipts = receipts[1:]

        push_receipt = PushAtelierCommitReceipt.from_result_bytes(
            bytes(receipts[0].result)
        )
        assert push_receipt.commit_oid == run_git(
            remote, "rev-parse", push_receipt.full_ref
        )
        assert push_receipt.parent == base
        assert push_receipt.candidate_tree == run_git(
            remote, "rev-parse", f"{push_receipt.commit_oid}^{{tree}}"
        )
        assert push_receipt.author == author
        assert push_receipt.committer == committer

        open_request = OpenPullRequest.from_canonical_bytes(
            final_intents[2].request.payload
        )
        assert open_request.head_branch.value == push_receipt.branch
        assert github.recorded_pull_requests()[0].branch == push_receipt.branch

        with runtime.engine.connect() as connection:
            connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
            connection.exec_driver_sql("DROP TRIGGER effect_receipts_no_delete")
            connection.execute(
                effect_receipts.delete().where(
                    effect_receipts.c.run_id == RUN.value,
                    effect_receipts.c.operation_name
                    == AdapterOperationName.PUSH_ATELIER_COMMIT.value,
                )
            )
            connection.execute(
                runs.update()
                .where(runs.c.run_id == RUN.value)
                .values(
                    state=RunState.STARTED.value,
                    current_node_id="open-pull-request",
                    terminal_hash=None,
                )
            )
            connection.commit()
            connection.exec_driver_sql("PRAGMA foreign_keys=ON")
        with (
            runtime.engine.begin() as connection,
            pytest.raises(RunEffectConflict, match="confirmed push receipt"),
        ):
            graph_action_intent(
                connection,
                RUN,
                workflow.revision_hash,
                (github.binding, push.binding),
                PROJECT,
            )
    finally:
        runtime.close()
