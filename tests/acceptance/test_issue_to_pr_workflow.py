"""The repository's issue-to-pr workflow runs from one issue order to one PR."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
import sqlalchemy as sa
from httpx import Response

from atelier2.adapters.candidate_store import CANDIDATE_STORE_DIRECTORY_NAME
from atelier2.adapters.dbos.agent_catalog import DbosAgentConfigurationCatalog
from atelier2.adapters.dbos.catalog_store import DbosCatalogStore
from atelier2.adapters.dbos.effect_store import intent_snapshot_from_record
from atelier2.adapters.dbos.run_store import DbosWaitAnswerer
from atelier2.adapters.dbos.runtime import DbosRuntime, DbosRuntimeSettings
from atelier2.adapters.dbos.schema import (
    agent_receipts_v2,
    effect_intents,
    effect_receipts,
    run_events,
    runs,
    tool_redemptions,
)
from atelier2.adapters.dbos.starter import DbosWorkflowRevisionPublisher
from atelier2.adapters.git_transport.effects import (
    GitRemote,
    GitTransportEffectAdapterFactory,
)
from atelier2.adapters.github.effects import GitHubEffectAdapterFactory
from atelier2.adapters.yaml_workflows import parse_workflow_document
from atelier2.api.openapi import API_PREFIX
from atelier2.application.answer_wait import UnanswerableWait, answer_wait_result
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
from atelier2.contracts.effect_markers import body_carries_request_hash
from atelier2.contracts.effect_requests import (
    GitCommitIdentity,
    OpenPullRequest,
    PushAtelierCommitReceipt,
)
from atelier2.contracts.effects import (
    AdapterRevision,
    EffectDestination,
)
from atelier2.contracts.executions import (
    NodeExecutionId,
    RunEventKind,
    SubmitWaitAnswerRequest,
    WaitAnswerActor,
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
    emitting,
    launching,
    publish_checked_model_registry,
)
from tests.scenarios.api import durable_api_client
from tests.scenarios.head_branch_pull_requests import FakeHeadBranchPullRequests
from tests.scenarios.issue_observation import FakeTrackerItemSource
from tests.scenarios.projects import declaring_verification, git_project, run_git
from tests.scenarios.run_waiting import wait_for_run_state
from tests.scenarios.runs import submit_wait_answer

WORKFLOW_PATH = Path("workflows/issue-to-pr.yaml")
BUDGET_PATH = Path("workflows/budgets/push-implement.json")
PROJECT = ProjectId("issue-to-pr-workflow")
ITEM = TrackerItemReference("gh:1232")
RUN = RunId("v3/issue-to-pr")
ORDER_NAME = "context"
BUILD_NODE = "build"
REVIEW_NODE = "review"
WAIT_NODE = "authorize_pr"
BUILDER_PROVIDER = ProviderId("exact")
REVIEWER_PROVIDER = ProviderId("other")
"""Two provider families -- this scenario's own arrangement, not `resolve_start_bindings`'s doing.

`resolve_start_bindings`, the graph-level start check, never reads
`family_differs_from`; only `cast_unbound_roles` does. The starter still runs
that cast ahead of `resolve_start_bindings` for every V3 start, including one
naming both roles explicitly, so a request colliding `build` and `review` on
one configuration is refused there first --
`test_a_start_refuses_the_same_configuration_bound_to_build_and_review` proves
it, at the real `POST /runs` door. `review`'s own `binding_constraint:
distinct_from: build` is a second, independent guarantee living inside
`resolve_start_bindings` itself: unreachable through that exact collision,
because the two roles can never share a configuration hash without also
sharing a provider, but it would still hold if a later document ever dropped
`family_differs_from` from this pair.
"""

CANDIDATE_FILE_NAME = "candidate.txt"
CANDIDATE_FILE_BYTES = b"what the builder changed\n"
BUILDER_SUMMARY = "Wrote the line the item asked for."
CANDIDATE_REPORT = json.dumps(
    {"summary": BUILDER_SUMMARY, "changed_paths": [CANDIDATE_FILE_NAME]}
).encode()
REVIEW_RESULT = json.dumps({"findings": [], "verdict": "approve"}).encode()
"""The reviewer's own bytes, deliberately unlike the builder's.

The pull request body must be traceable to the builder's report specifically;
identical bytes would let a body composed from whichever agent ran last pass.
"""
RELEASE_ANSWER = b'"open-pr"'
REFUSED_ANSWER = b'"cancel"'
VERIFICATION_OUTPUT = b"green"

_EDIT_THEN_REPORT = (
    "import os,pathlib,sys;"
    "pathlib.Path(sys.argv[1]).write_bytes(bytes.fromhex(sys.argv[2]));"
    "os.write(1,bytes.fromhex(sys.argv[3]))"
)


def _project_and_remote(root: Path, verification_record: Path) -> tuple[Path, Path]:
    """A project that states how it is verified, and the remote its push reaches.

    The declared command reads the file the builder writes into its lease, so
    what the record holds afterwards proves the check ran on the changed tree
    rather than on the pinned one.
    """
    project = root / "project"
    git_project(
        project,
        declaring_verification(
            [
                "/bin/sh",
                "-c",
                (
                    f"cat {CANDIDATE_FILE_NAME} > {verification_record}; "
                    f"printf '{VERIFICATION_OUTPUT.decode('ascii')}'"
                ),
            ]
        ),
    )
    remote = root / "remote.git"
    run_git(root, "init", "--bare", "--quiet", str(remote))
    run_git(project, "push", "--quiet", str(remote), "HEAD:refs/heads/main")
    return project, remote


def _executors() -> tuple[
    RecordingAgentExecutorFactoryV2, RecordingAgentExecutorFactoryV2
]:
    """One provider that edits its lease and reports it, one that only judges."""
    return (
        RecordingAgentExecutorFactoryV2(
            BUILDER_PROVIDER.value,
            f"{BUILDER_PROVIDER.value}/v1",
            f"{BUILDER_PROVIDER.value}-operation",
            b"",
            capability_set=frozenset({AgentExecutionCapability.HEADLESS_WITH_TOOLS}),
            command=launching(
                sys.executable,
                "-c",
                _EDIT_THEN_REPORT,
                CANDIDATE_FILE_NAME,
                CANDIDATE_FILE_BYTES.hex(),
                CANDIDATE_REPORT.hex(),
            ),
        ),
        RecordingAgentExecutorFactoryV2(
            REVIEWER_PROVIDER.value,
            f"{REVIEWER_PROVIDER.value}/v1",
            f"{REVIEWER_PROVIDER.value}-operation",
            b"",
            command=emitting(REVIEW_RESULT),
        ),
    )


def _runtime(
    tmp_path: Path,
) -> tuple[
    DbosRuntime,
    GitHubEffectAdapterFactory,
    Path,
    Path,
    RecordingAgentExecutorFactoryV2,
]:
    """The runtime this workflow's tests share: real git, fake GitHub, fake agents.

    Returns the runtime, the recording GitHub adapter, the bare remote the push
    reaches, the file the declared verification writes the candidate into, and
    the reviewer's own executor -- which is where the job that reviewer was
    actually handed can be read -- every one of them a caller may need to assert
    against once a run has moved.
    """
    verification_record = tmp_path / "verification.txt"
    builder, reviewer = _executors()
    project, remote = _project_and_remote(tmp_path, verification_record)
    github = GitHubEffectAdapterFactory(
        tmp_path / "github.sqlite",
        AdapterRevision("github-open-pr-v1"),
        EffectDestination("platform"),
    )
    push = GitTransportEffectAdapterFactory(
        tmp_path / CANDIDATE_STORE_DIRECTORY_NAME,
        GitRemote("local-issue-to-pr-test", str(remote)),
        AdapterRevision("git-push-v1"),
        EffectDestination("git"),
        FakeHeadBranchPullRequests(),
    )
    runtime = DbosRuntime(
        DbosRuntimeSettings(
            tmp_path / "atelier.sqlite",
            "issue-to-pr-workflow-test",
            agent_scratch_root=agent_scratch_root(tmp_path),
            project_id=PROJECT,
            bootstrap_project_root=project,
        ),
        EffectAdapterRegistry(
            (
                EffectAdapterRegistration(AdapterOperationName.OPEN_PR, github),
                EffectAdapterRegistration(
                    AdapterOperationName.PUSH_ATELIER_COMMIT, push
                ),
            )
        ),
        (builder, reviewer),
    )
    runtime.initialize_storage()
    return runtime, github, remote, verification_record, reviewer


def _publish_workflow(
    runtime: DbosRuntime,
) -> tuple[
    WorkflowRevision,
    AgentBindingSet,
    tuple[GitCommitIdentity, GitCommitIdentity],
]:
    # The live revisions pin the operator as author and the pushing node's model
    # as committer (#883, operator ruling 30.08.2026); the shipped document pins
    # that same model, so reproducing this exact pair is what makes the derived
    # grant hash equal the one it names.
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
    verification_grant = PublishedRevision(
        RevisionKind.TOOL,
        json.dumps(
            {"capability": ToolGrantCapability.RUN_PROJECT_VERIFICATION.value},
            sort_keys=True,
            separators=(",", ":"),
        ).encode(),
    )
    store = DbosCatalogStore(runtime.engine)
    for revision in (
        PublishedRevision(RevisionKind.SCHEMA, WORK_ITEM_ORDER_SCHEMA_DOCUMENT),
        PublishedRevision(
            RevisionKind.SCHEMA,
            Path("workflows/schemas/issue_to_pr_candidate_report.json").read_bytes(),
        ),
        PublishedRevision(
            RevisionKind.SCHEMA,
            Path("workflows/schemas/code_review_result.json").read_bytes(),
        ),
        PublishedRevision(
            RevisionKind.SCHEMA,
            Path("workflows/schemas/issue_to_pr_release_decision.json").read_bytes(),
        ),
        PublishedRevision(RevisionKind.BUDGET_POLICY, BUDGET_PATH.read_bytes()),
        push_operation,
        PublishedRevision(RevisionKind.ADAPTER_OPERATION, b'{"operation":"open-pr"}'),
        push_grant,
        verification_grant,
    ):
        published = store.publish_revision(revision)
        assert isinstance(
            published, (PublishedRevisionCreated, PublishedRevisionExisting)
        ), published

    shipped_document = WORKFLOW_PATH.read_bytes()
    assert push_grant.revision_hash.value.encode() in shipped_document
    assert verification_grant.revision_hash.value.encode() in shipped_document
    builder = parse_workflow_document(shipped_document).node(BUILD_NODE)
    assert isinstance(builder, AgentNodeV3)
    assert builder.model == pushing_model
    workflow = WorkflowRevision(shipped_document)
    DbosWorkflowRevisionPublisher(runtime.engine).publish(workflow)

    catalog = DbosAgentConfigurationCatalog(
        runtime.engine, runtime.agent_executor_registry
    )
    bindings: list[AgentBinding] = []
    for role, provider, capability in (
        ("builder", BUILDER_PROVIDER, AgentExecutionCapability.HEADLESS_WITH_TOOLS),
        ("reviewer", REVIEWER_PROVIDER, AgentExecutionCapability.HEADLESS),
    ):
        auth = AuthProfileRevision(
            f"{role}-profile", 1, provider, AuthMode.SUBSCRIPTION
        )
        assert isinstance(
            catalog.publish_auth_profile_revision(auth), AuthProfileRevisionCreated
        )
        configuration = AgentConfigurationRevision(
            role,
            auth.revision_hash,
            AgentExecutorRevision(f"{provider.value}/v1"),
            capability,
            AgentConfigurationRevisionFormatVersion.V2,
        )
        assert isinstance(
            catalog.publish_agent_configuration_revision(configuration),
            AgentConfigurationRevisionCreated,
        )
        publish_checked_model_registry(runtime.engine, provider, (configuration,))
        bindings.append(AgentBinding(AgentRole(role), configuration.revision_hash))
    return workflow, AgentBindingSet(tuple(bindings)), (author, committer)


def _start(
    runtime: DbosRuntime, workflow: WorkflowRevision, bindings: AgentBindingSet
) -> Response:
    """Start the run the way the head does: the bindings, and the issue alone."""
    item = ObservedWorkItemRevision(
        ITEM,
        WorkItemKind.ISSUE,
        b"Write the line this run is for.",
        WorkItemChangeMarker("issue-1232-v1"),
        RecordedAt("2026-09-04T12:00:00Z"),
    )
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
            "run_id": RUN.value,
            "workflow_revision_hash": workflow.revision_hash.value,
            "agent_bindings": [
                {
                    "role": binding.role.value,
                    "agent_configuration_revision_hash": (
                        binding.agent_configuration_revision_hash.value
                    ),
                }
                for binding in bindings.bindings
            ],
            "orders": [{"name": ORDER_NAME, "work_item": ITEM.value}],
        },
    )


def _assert_the_reviewer_read_the_candidate_diff(
    built: bytes | None, reviewer: RecordingAgentExecutorFactoryV2
) -> None:
    """The patch the atelier read is in the value, and in the reviewer's own job.

    Read from the completion event rather than from the builder's answer,
    because the builder never wrote it: the atelier read the tree the builder
    left and put the patch into the value the node completed with. What proves
    the reviewer actually got it is the job that executor was handed -- the same
    text a real provider would have been given, carrying that whole value as a
    produced value travels: JSON, its own newlines escaped.
    """

    assert built is not None
    value = json.loads(built)
    assert value["summary"] == BUILDER_SUMMARY
    diff = value["candidate_diff"]
    assert f"+++ b/{CANDIDATE_FILE_NAME}" in diff
    assert f"+{CANDIDATE_FILE_BYTES.decode('utf-8')}" in diff
    assert reviewer.opened is not None
    (judged,) = reviewer.opened.requests
    assert built.decode("utf-8") in judged.job_bytes.decode("utf-8")


@pytest.mark.proves("issue-to-pr-builds-reviews-waits-and-opens-the-pull-request")
@pytest.mark.proves("issue-to-pr-shows-the-reviewer-the-candidates-own-diff")
def test_issue_to_pr_builds_reviews_waits_then_opens_the_pull_request(
    tmp_path: Path,
) -> None:
    """The whole shipped chain from one work-item order, driven with fake agents.

    The order carries nothing but the issue reference: the builder's brief, the
    reviewer's contract and the branch the push derives all come from that one
    observed item. The commit exists before the review, because the push is the
    builder attempt's own continuation; what the Wait releases is the pull
    request, and only the exact release opens it.
    """
    runtime, github, remote, verification_record, reviewer = _runtime(tmp_path)
    try:
        workflow, bindings, (author, committer) = _publish_workflow(runtime)
        response = _start(runtime, workflow, bindings)
        assert response.status_code == 201, response.text
        runtime.launch()

        wait_for_run_state(runtime.engine, RUN, RunState.WAITING_INPUT)
        with runtime.engine.connect() as connection:
            push_intent = intent_snapshot_from_record(
                connection.execute(sa.select(effect_intents)).mappings().one()
            ).intent
            redemption = (
                connection.execute(sa.select(tool_redemptions)).mappings().one()
            )
        assert push_intent.binding.operation_name is (
            AdapterOperationName.PUSH_ATELIER_COMMIT
        )
        assert str(redemption["node_id"]) == BUILD_NODE
        assert str(redemption["capability"]) == (
            ToolGrantCapability.RUN_PROJECT_VERIFICATION.value
        )
        assert int(redemption["exit_code"]) == 0
        assert verification_record.read_bytes() == CANDIDATE_FILE_BYTES
        assert github.recorded_pull_requests() == ()

        with runtime.engine.connect() as connection:
            reviewed = (
                connection.execute(
                    sa.select(agent_receipts_v2.c.output_bytes).where(
                        agent_receipts_v2.c.run_id == RUN.value,
                        agent_receipts_v2.c.node_id == REVIEW_NODE,
                    )
                )
                .scalars()
                .one()
            )
            waiting_node = connection.scalar(
                sa.select(runs.c.current_node_id).where(runs.c.run_id == RUN.value)
            )
            waiting_question = connection.scalar(
                sa.select(run_events.c.payload).where(
                    run_events.c.run_id == RUN.value,
                    run_events.c.node_id == WAIT_NODE,
                    run_events.c.event_kind == RunEventKind.WAITING_INPUT.value,
                )
            )
            built = connection.scalar(
                sa.select(run_events.c.payload).where(
                    run_events.c.run_id == RUN.value,
                    run_events.c.node_id == BUILD_NODE,
                    run_events.c.event_kind == RunEventKind.AGENT_COMPLETED.value,
                )
            )
        assert bytes(reviewed) == REVIEW_RESULT
        _assert_the_reviewer_read_the_candidate_diff(built, reviewer)
        assert waiting_node == WAIT_NODE
        assert github.recorded_pull_requests() == ()
        assert waiting_question is not None
        assert REVIEW_RESULT.decode("utf-8") in bytes(waiting_question).decode("utf-8")

        wait_execution = NodeExecutionId.for_node(
            RUN, workflow.revision_hash, WAIT_NODE
        )
        refused = answer_wait_result(
            RUN,
            workflow.revision_hash,
            WAIT_NODE,
            wait_execution,
            WaitAnswerActor.OPERATOR,
            REFUSED_ANSWER,
            DbosWaitAnswerer(runtime.engine, runtime.settings.application_version),
        )
        assert isinstance(refused, UnanswerableWait), refused
        wait_for_run_state(runtime.engine, RUN, RunState.WAITING_INPUT)
        assert github.recorded_pull_requests() == ()

        submit_wait_answer(
            runtime.engine,
            runtime.settings.application_version,
            SubmitWaitAnswerRequest(
                RUN,
                workflow.revision_hash,
                WAIT_NODE,
                wait_execution,
                WaitAnswerActor.OPERATOR,
                RELEASE_ANSWER,
            ),
        )
        wait_for_run_state(runtime.engine, RUN, RunState.COMPLETED)

        with runtime.engine.connect() as connection:
            intents = tuple(
                intent_snapshot_from_record(row).intent
                for row in connection.execute(
                    sa.select(effect_intents).order_by(sa.literal_column("rowid"))
                ).mappings()
            )
            receipts = connection.execute(
                sa.select(
                    effect_receipts.c.operation_name, effect_receipts.c.result
                ).order_by(sa.literal_column("rowid"))
            ).all()
        assert [intent.binding.operation_name for intent in intents] == [
            AdapterOperationName.PUSH_ATELIER_COMMIT,
            AdapterOperationName.OPEN_PR,
        ]
        assert [receipt.operation_name for receipt in receipts] == [
            AdapterOperationName.PUSH_ATELIER_COMMIT.value,
            AdapterOperationName.OPEN_PR.value,
        ]

        push_receipt = PushAtelierCommitReceipt.from_result_bytes(
            bytes(receipts[0].result)
        )
        assert push_receipt.author == author
        assert push_receipt.committer == committer
        assert push_receipt.commit_oid == run_git(
            remote, "rev-parse", push_receipt.full_ref
        )
        opened = OpenPullRequest.from_canonical_bytes(intents[1].request.payload)
        assert opened.head_branch.value == push_receipt.branch
        assert opened.work_item_reference == ITEM

        (recorded,) = github.recorded_pull_requests()
        assert recorded.branch == push_receipt.branch
        assert body_carries_request_hash(
            recorded.body, intents[1].request.request_hash.value
        )
        assert BUILDER_SUMMARY in recorded.body
        assert CANDIDATE_FILE_NAME in recorded.body
        assert REVIEW_RESULT.decode("utf-8") not in recorded.body
    finally:
        runtime.close()


def test_a_start_refuses_the_same_configuration_bound_to_build_and_review(
    tmp_path: Path,
) -> None:
    """Binding one configuration to both `build` and `review` is refused at the door.

    A same-configuration collision between these two roles can never reach
    `resolve_start_bindings`'s own `binding_constraint` check here: the
    starter's always-running `cast_unbound_roles` seam refuses it first,
    because the two roles can never share a configuration hash without also
    sharing a provider, and `review` declares `family_differs_from: builder`.
    What this proves is the shipped document's actual behavior at the real
    door -- not which of its two independent guarantees answers.
    """
    runtime, _, _, _, _ = _runtime(tmp_path)
    try:
        workflow, bindings, _ = _publish_workflow(runtime)
        builder_binding = next(
            binding for binding in bindings.bindings if binding.role.value == "builder"
        )
        colliding = AgentBindingSet(
            tuple(
                AgentBinding(
                    binding.role, builder_binding.agent_configuration_revision_hash
                )
                for binding in bindings.bindings
            )
        )

        response = _start(runtime, workflow, colliding)

        assert response.status_code == 422, response.text
        problem = response.json()
        assert problem["type"].endswith(":uncast-agent-roles")
        (uncast_reviewer,) = problem["uncast_roles"]
        assert uncast_reviewer["role"] == "reviewer"
        assert uncast_reviewer["reason"] == "family-difference-unavailable"
        assert uncast_reviewer["family_differs_from"] == "builder"
        with runtime.engine.connect() as connection:
            assert connection.scalar(sa.select(sa.func.count()).select_from(runs)) == 0
    finally:
        runtime.close()
