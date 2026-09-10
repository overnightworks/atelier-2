"""The repository's head-loop workflow drives one item from re-questioning to a PR.

The circle the head walks by hand is written down here as catalog data alone:
a planner re-questions the item, the operator pulls it, a reviewer judges the
plan, a workspace-tool builder implements it, an independently cast reviewer
judges the candidate, and the pull request opens once the operator releases it.
That every pinned schema, grant, budget and operation revision resolves is
proved by starting the shipped document below; that it is shipped as a
publishable, executable catalog document is proved for every workflow in
`tests/domain/test_authored_workflows.py`.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path

import sqlalchemy as sa
from httpx import Response

from atelier2.adapters.candidate_store import CANDIDATE_STORE_DIRECTORY_NAME
from atelier2.adapters.dbos.agent_catalog import DbosAgentConfigurationCatalog
from atelier2.adapters.dbos.runtime import DbosRuntime, DbosRuntimeSettings
from atelier2.adapters.dbos.schema import agent_receipts_v2, run_events
from atelier2.adapters.dbos.starter import DbosWorkflowRevisionPublisher
from atelier2.adapters.git_transport.effects import (
    GitRemote,
    GitTransportEffectAdapterFactory,
)
from atelier2.adapters.github.effects import GitHubEffectAdapterFactory
from atelier2.adapters.yaml_workflows import parse_executable_workflow_document
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
from atelier2.contracts.effect_requests import GitCommitIdentity
from atelier2.contracts.effects import AdapterRevision, EffectDestination
from atelier2.contracts.executions import (
    NodeExecutionId,
    RunEventKind,
    SubmitWaitAnswerRequest,
    WaitAnswerActor,
)
from atelier2.contracts.host_configuration import ProjectId
from atelier2.contracts.queue_projection import TrackerItemReference
from atelier2.contracts.revisions_v3 import PublishedRevision, RevisionKind
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
    ObservedWorkItemRevision,
    WorkItemChangeMarker,
    WorkItemKind,
)
from atelier2.contracts.workflow_executability import (
    what_a_v3_document_still_waits_for,
)
from atelier2.contracts.workflows_v3 import is_linear_chain
from atelier2.ports.agent_configurations import (
    AgentConfigurationRevisionCreated,
    AuthProfileRevisionCreated,
)
from atelier2.ports.effects import EffectAdapterRegistration, EffectAdapterRegistry
from atelier2.ports.issue_observation import WorkItemRevisionObserved
from tests.scenarios.agents import (
    RecordingAgentExecutorFactoryV2,
    agent_scratch_root,
    answering_each_execution,
    emitting,
    publish_checked_model_registry,
    working_and_emitting,
)
from tests.scenarios.api import durable_api_client
from tests.scenarios.head_branch_pull_requests import FakeHeadBranchPullRequests
from tests.scenarios.issue_observation import FakeTrackerItemSource
from tests.scenarios.projects import declaring_verification, git_project, run_git
from tests.scenarios.run_waiting import wait_for_run_state
from tests.scenarios.runs import publish_pinned_revisions, submit_wait_answer
from tests.scenarios.work_item_claims import fake_agent_claim_executable

WORKFLOW_PATH = Path("workflows/head-loop.yaml")
BUDGET_PATH = Path("workflows/budgets/push-implement.json")
SCHEMA_DIRECTORY = Path("workflows/schemas")
PINNED_SCHEMA_NAMES = (
    "nonempty_string",
    "requestion_result",
    "head_loop_pull_decision",
    "plan_review_result",
    "issue_to_pr_candidate_report",
    "code_review_result",
    "issue_to_pr_release_decision",
)

REQUESTION_NODE = "requestion"
PULL_WAIT = "pull"
PLAN_REVIEW_NODE = "plan_review"
REVIEW_NODE = "review"
RELEASE_WAIT = "authorize_pr"
NODES_IN_ORDER = (
    REQUESTION_NODE,
    PULL_WAIT,
    PLAN_REVIEW_NODE,
    "build",
    REVIEW_NODE,
    RELEASE_WAIT,
    "open_pr",
)

PROJECT = ProjectId("head-loop-workflow")
ITEM = TrackerItemReference("gh:1232")
RUN = RunId("v3/head-loop")
ITEM_ORDER = "context"
OWNER_DOCUMENTS_ORDER = "owner_documents"
OWNER_DOCUMENTS = "AGENTS.md: grow the repository only to remove a named problem."

PLANNER_PROVIDER = ProviderId("planner-family")
REVIEWER_PROVIDER = ProviderId("reviewer-family")
BUILDER_PROVIDER = ProviderId("builder-family")
ROLES = (
    ("planner", PLANNER_PROVIDER, AgentExecutionCapability.HEADLESS),
    ("reviewer", REVIEWER_PROVIDER, AgentExecutionCapability.HEADLESS),
    ("builder", BUILDER_PROVIDER, AgentExecutionCapability.HEADLESS_WITH_TOOLS),
)

OVERTAKEN = json.dumps(
    {
        "verdict": "overtaken",
        "reason": "The head would close this item as already landed.",
        "evidence": "The owner document already carries the sentence it asks for.",
    }
).encode()
STILL_A_PROBLEM = json.dumps(
    {
        "verdict": "still_a_problem",
        "reason": "Nothing in the owner documents answers the item yet.",
        "evidence": "The item asks for a line no owner document carries.",
    }
).encode()
PASSED_PLAN = json.dumps(
    {
        "risks": [],
        "plan": [
            {
                "step": "Write the line the item asks for",
                "files": ["one.txt"],
                "test": "the declared verification",
            }
        ],
        "verdict": "pass",
    }
).encode()
APPROVING_REVIEW = json.dumps({"findings": [], "verdict": "approve"}).encode()
REVISING_REVIEW = json.dumps(
    {
        "findings": [
            {
                "file": "candidate.txt",
                "line": 1,
                "severity": "medium",
                "text": "The line says nothing about why it was added.",
            }
        ],
        "verdict": "revise",
    }
).encode()

PULL_ANSWER = b'"pull"'
RELEASE_ANSWER = b'"open-pr"'

CANDIDATE_FILE_NAME = "candidate.txt"
CANDIDATE_FILE_TEXT = "what the builder changed\n"
BUILDER_SUMMARY = "Wrote the line the item asked for."
CANDIDATE_REPORT = json.dumps(
    {"summary": BUILDER_SUMMARY, "changed_paths": [CANDIDATE_FILE_NAME]}
).encode()

_QUESTION_TIMEOUT_SECONDS = 30.0
_QUESTION_POLL_SECONDS = 0.025


@dataclass(frozen=True)
class _Stage:
    """One arranged head-loop run: the runtime it drives and the doubles it reads."""

    runtime: DbosRuntime
    github: GitHubEffectAdapterFactory
    builder: RecordingAgentExecutorFactoryV2
    workflow: WorkflowRevision


def _executors(
    requestion_result: bytes, review_result: bytes
) -> tuple[RecordingAgentExecutorFactoryV2, ...]:
    """One provider per role, each answering what its own nodes owe.

    `plan_review` and `review` are one role, so one cast reviewer answers both;
    it says which of the two schemas it is filling by the node it was handed.
    """
    return (
        RecordingAgentExecutorFactoryV2(
            PLANNER_PROVIDER.value,
            f"{PLANNER_PROVIDER.value}/v1",
            f"{PLANNER_PROVIDER.value}-operation",
            b"",
            command=emitting(requestion_result),
        ),
        RecordingAgentExecutorFactoryV2(
            REVIEWER_PROVIDER.value,
            f"{REVIEWER_PROVIDER.value}/v1",
            f"{REVIEWER_PROVIDER.value}-operation",
            b"",
            command=answering_each_execution(
                {
                    (PLAN_REVIEW_NODE, FIRST_ROUND_ORDINAL): PASSED_PLAN,
                    (REVIEW_NODE, FIRST_ROUND_ORDINAL): review_result,
                }
            ),
        ),
        RecordingAgentExecutorFactoryV2(
            BUILDER_PROVIDER.value,
            f"{BUILDER_PROVIDER.value}/v1",
            f"{BUILDER_PROVIDER.value}-operation",
            b"",
            capability_set=frozenset({AgentExecutionCapability.HEADLESS_WITH_TOOLS}),
            command=working_and_emitting(
                CANDIDATE_REPORT, CANDIDATE_FILE_NAME, CANDIDATE_FILE_TEXT
            ),
        ),
    )


def _runtime(
    tmp_path: Path, executors: tuple[RecordingAgentExecutorFactoryV2, ...]
) -> tuple[DbosRuntime, GitHubEffectAdapterFactory]:
    """The runtime this workflow's tests share: real git, fake GitHub, fake agents."""
    project = tmp_path / "project"
    git_project(project, declaring_verification(["/bin/sh", "-c", "printf green"]))
    remote = tmp_path / "remote.git"
    run_git(tmp_path, "init", "--bare", "--quiet", str(remote))
    run_git(project, "push", "--quiet", str(remote), "HEAD:refs/heads/main")

    github = GitHubEffectAdapterFactory(
        tmp_path / "github.sqlite",
        AdapterRevision("github-open-pr-v1"),
        EffectDestination("platform"),
    )
    push = GitTransportEffectAdapterFactory(
        tmp_path / CANDIDATE_STORE_DIRECTORY_NAME,
        GitRemote("local-head-loop-test", str(remote)),
        AdapterRevision("git-push-v1"),
        EffectDestination("git"),
        FakeHeadBranchPullRequests(),
    )
    runtime = DbosRuntime(
        DbosRuntimeSettings(
            tmp_path / "atelier.sqlite",
            "head-loop-workflow-test",
            agent_scratch_root=agent_scratch_root(tmp_path),
            project_id=PROJECT,
            bootstrap_project_root=project,
            aco_executable=fake_agent_claim_executable(tmp_path),
        ),
        EffectAdapterRegistry(
            (
                EffectAdapterRegistration(AdapterOperationName.OPEN_PR, github),
                EffectAdapterRegistration(
                    AdapterOperationName.PUSH_ATELIER_COMMIT, push
                ),
            )
        ),
        executors,
    )
    runtime.initialize_storage()
    return runtime, github


def _push_operation() -> PublishedRevision:
    """The push operation whose hash the shipped push grant names.

    The live revision pins the operator as author and one exact model as
    committer, and the shipped grant's hash is derived from those bytes, so a
    start only resolves the grant this document pins when the same pair is
    republished here.
    """
    address = "44832414+FlexOr2@users.noreply.github.com"
    return PublishedRevision(
        RevisionKind.ADAPTER_OPERATION,
        json.dumps(
            {
                "operation": AdapterOperationName.PUSH_ATELIER_COMMIT.value,
                "author": GitCommitIdentity("Felix Hummert", address).as_json(),
                "committer": GitCommitIdentity("Grok 4.6", address).as_json(),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode(),
    )


def _push_grant(operation: PublishedRevision) -> PublishedRevision:
    return PublishedRevision(
        RevisionKind.TOOL,
        json.dumps(
            {
                "capability": ToolGrantCapability.PUSH_ATELIER_COMMIT.value,
                "operation": {
                    "ref": "push-atelier-commit",
                    "revision": operation.revision_hash.value,
                },
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode(),
    )


def _publish_catalog(runtime: DbosRuntime) -> WorkflowRevision:
    """Everything the shipped document pins, then the document itself."""
    push_operation = _push_operation()
    publish_pinned_revisions(
        runtime.engine,
        PublishedRevision(RevisionKind.SCHEMA, WORK_ITEM_ORDER_SCHEMA_DOCUMENT),
        *(
            PublishedRevision(
                RevisionKind.SCHEMA,
                (SCHEMA_DIRECTORY / f"{name}.json").read_bytes(),
            )
            for name in PINNED_SCHEMA_NAMES
        ),
        PublishedRevision(RevisionKind.BUDGET_POLICY, BUDGET_PATH.read_bytes()),
        push_operation,
        PublishedRevision(RevisionKind.ADAPTER_OPERATION, b'{"operation":"open-pr"}'),
        _push_grant(push_operation),
        PublishedRevision(
            RevisionKind.TOOL,
            json.dumps(
                {"capability": ToolGrantCapability.RUN_PROJECT_VERIFICATION.value},
                separators=(",", ":"),
            ).encode(),
        ),
    )
    workflow = WorkflowRevision(WORKFLOW_PATH.read_bytes())
    DbosWorkflowRevisionPublisher(runtime.engine).publish(workflow)
    return workflow


def _publish_bindings(runtime: DbosRuntime) -> AgentBindingSet:
    """One published configuration per declared role, on its own provider family."""
    catalog = DbosAgentConfigurationCatalog(
        runtime.engine, runtime.agent_executor_registry
    )
    bindings: list[AgentBinding] = []
    for role, provider, capability in ROLES:
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
    return AgentBindingSet(tuple(bindings))


def _start(stage: _Stage, bindings: AgentBindingSet) -> Response:
    """Start the run the way the head does: the bindings, the issue, the owners."""
    item = ObservedWorkItemRevision(
        ITEM,
        WorkItemKind.ISSUE,
        b"Write the line this run is for.\n\n## Bereich\none.txt\n",
        WorkItemChangeMarker("issue-1232-v1"),
        RecordedAt("2026-09-10T12:00:00Z"),
    )
    client = durable_api_client(
        stage.runtime,
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
            "workflow_revision_hash": stage.workflow.revision_hash.value,
            "agent_bindings": [
                {
                    "role": binding.role.value,
                    "agent_configuration_revision_hash": (
                        binding.agent_configuration_revision_hash.value
                    ),
                }
                for binding in bindings.bindings
            ],
            "orders": [
                {"name": ITEM_ORDER, "work_item": ITEM.value},
                {"name": OWNER_DOCUMENTS_ORDER, "value": OWNER_DOCUMENTS},
            ],
        },
    )


def _started_stage(
    tmp_path: Path, *, requestion_result: bytes, review_result: bytes
) -> _Stage:
    """A launched run of the shipped document, standing at its first question."""
    planner, reviewer, builder = _executors(requestion_result, review_result)
    runtime, github = _runtime(tmp_path, (planner, reviewer, builder))
    workflow = _publish_catalog(runtime)
    stage = _Stage(runtime, github, builder, workflow)
    response = _start(stage, _publish_bindings(runtime))
    assert response.status_code == 201, response.text
    runtime.launch()
    return stage


def _question_at(stage: _Stage, node_id: str) -> str:
    """The composed question this wait node durably asked, once it has asked it.

    Read from the pause event rather than from the run's current node, because
    the event only ever accumulates: a caller answering one wait and waiting
    for the next cannot race the run back out of `WAITING_INPUT` and in again.
    """
    deadline = time.monotonic() + _QUESTION_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        with stage.runtime.engine.connect() as connection:
            payload = connection.scalar(
                sa.select(run_events.c.payload).where(
                    run_events.c.run_id == RUN.value,
                    run_events.c.node_id == node_id,
                    run_events.c.event_kind == RunEventKind.WAITING_INPUT.value,
                )
            )
        if payload is not None:
            return bytes(payload).decode("utf-8")
        time.sleep(_QUESTION_POLL_SECONDS)
    raise TimeoutError(f"node {node_id!r} never asked its question")


def _answer(stage: _Stage, node_id: str, answer: bytes) -> None:
    submit_wait_answer(
        stage.runtime.engine,
        stage.runtime.settings.application_version,
        SubmitWaitAnswerRequest(
            RUN,
            stage.workflow.revision_hash,
            node_id,
            NodeExecutionId.for_node(RUN, stage.workflow.revision_hash, node_id),
            WaitAnswerActor.OPERATOR,
            answer,
        ),
    )


def _nodes_that_ran_an_agent(stage: _Stage) -> set[str]:
    with stage.runtime.engine.connect() as connection:
        return set(
            connection.execute(
                sa.select(agent_receipts_v2.c.node_id).where(
                    agent_receipts_v2.c.run_id == RUN.value
                )
            ).scalars()
        )


def _job_handed_to_the_builder(stage: _Stage) -> str:
    assert stage.builder.opened is not None
    (built,) = stage.builder.opened.requests
    return built.job_bytes.decode("utf-8")


def test_the_head_loop_document_is_one_chain_of_seven_nodes() -> None:
    """The shipped circle is a line of the seven nodes, with nothing left unbound."""
    graph = parse_executable_workflow_document(WORKFLOW_PATH.read_bytes())

    assert tuple(node.id for node in graph.nodes) == NODES_IN_ORDER
    assert is_linear_chain(graph)
    assert what_a_v3_document_still_waits_for(graph) is None


def test_an_overtaken_requestion_stands_at_the_pull_wait_before_any_builder_starts(
    tmp_path: Path,
) -> None:
    """The head is asked to pull the item before anything is built for it.

    The planner's answer is the whole of the question the operator sees, and
    the run holds there: no plan is reviewed, no workspace is leased, and no
    commit exists, so refusing costs nothing but a cancelled run.
    """
    stage = _started_stage(
        tmp_path, requestion_result=OVERTAKEN, review_result=APPROVING_REVIEW
    )
    try:
        question = _question_at(stage, PULL_WAIT)

        assert OVERTAKEN.decode("utf-8") in question
        assert _nodes_that_ran_an_agent(stage) == {REQUESTION_NODE}
        assert stage.github.recorded_pull_requests() == ()
    finally:
        stage.runtime.close()


def test_the_pulled_item_reaches_the_builder_with_the_reviewed_plan(
    tmp_path: Path,
) -> None:
    """Answering the pull sends the item through plan review and into the build.

    The builder can only hold the reviewed plan if `plan_review` answered
    first, so the job it was handed is where that order is read.
    """
    stage = _started_stage(
        tmp_path, requestion_result=STILL_A_PROBLEM, review_result=APPROVING_REVIEW
    )
    try:
        _question_at(stage, PULL_WAIT)
        _answer(stage, PULL_WAIT, PULL_ANSWER)
        _question_at(stage, RELEASE_WAIT)

        assert PASSED_PLAN.decode("utf-8") in _job_handed_to_the_builder(stage)
    finally:
        stage.runtime.close()


def test_a_revising_review_stands_at_the_release_wait_without_a_pull_request(
    tmp_path: Path,
) -> None:
    """A revise ends at the operator's release question, and opens nothing.

    There is no fix round in this document yet, so what a revise buys is the
    chance to cancel the run before the pull request exists.
    """
    stage = _started_stage(
        tmp_path, requestion_result=STILL_A_PROBLEM, review_result=REVISING_REVIEW
    )
    try:
        _question_at(stage, PULL_WAIT)
        _answer(stage, PULL_WAIT, PULL_ANSWER)

        assert REVISING_REVIEW.decode("utf-8") in _question_at(stage, RELEASE_WAIT)
        assert stage.github.recorded_pull_requests() == ()
    finally:
        stage.runtime.close()


def test_the_released_candidate_opens_the_pull_request_from_the_builders_report(
    tmp_path: Path,
) -> None:
    """The released run opens one pull request whose body is the builder's report."""
    stage = _started_stage(
        tmp_path, requestion_result=STILL_A_PROBLEM, review_result=APPROVING_REVIEW
    )
    try:
        _question_at(stage, PULL_WAIT)
        _answer(stage, PULL_WAIT, PULL_ANSWER)
        _question_at(stage, RELEASE_WAIT)
        _answer(stage, RELEASE_WAIT, RELEASE_ANSWER)
        wait_for_run_state(stage.runtime.engine, RUN, RunState.COMPLETED)

        (opened,) = stage.github.recorded_pull_requests()
        assert BUILDER_SUMMARY in opened.body
        assert CANDIDATE_FILE_NAME in opened.body
        assert APPROVING_REVIEW.decode("utf-8") not in opened.body
    finally:
        stage.runtime.close()
