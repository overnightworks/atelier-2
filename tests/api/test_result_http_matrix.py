from __future__ import annotations

import base64
import hashlib
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import cast

import pytest
from fastapi.testclient import TestClient

from atelier2.adapters.markdown_agent_definitions import (
    parse_agent_definition,
    render_agent_definition,
)
from atelier2.adapters.yaml_workflows import parse_executable_workflow_document
from atelier2.api.app import create_app
from atelier2.api.context import ApiPorts
from atelier2.api.openapi import CATALOG_LINEAGES_PATH
from atelier2.contracts.agents import AgentBindingSet
from atelier2.contracts.catalog_v3 import (
    CatalogLineageDisplayName,
    CatalogLineageId,
)
from atelier2.contracts.effects import (
    AdapterOperationalIdentity,
    AdapterRevision,
    CanonicalRequest,
    EffectBinding,
    EffectDestination,
    EffectIntent,
    EffectIntentSnapshot,
    EffectIntentState,
    EffectIntentStateVersion,
    LogicalEffectKey,
    OperatorAuthoritativeAbsence,
    ReconcileActor,
    ReconcileCommand,
    ReconcileCommandId,
    ReconcileCommandSnapshot,
    ReconcileCommandState,
)
from atelier2.contracts.executions import (
    NodeExecutionId,
    SubmitWaitAnswerRequest,
    WaitAnswer,
    WaitAnswerActor,
    WaitAnswerSnapshot,
    WaitAnswerState,
)
from atelier2.contracts.revisions_v3 import (
    PublishedRevision,
    PublishedRevisionHash,
    RevisionKind,
)
from atelier2.contracts.run_bindings import RunV3
from atelier2.contracts.run_configuration_v3 import RunConfigurationRevisionHash
from atelier2.contracts.run_projections import (
    RunPage,
    RunProjection,
    WaitingReconciliationProjection,
)
from atelier2.contracts.runs import (
    RunId,
    RunState,
    WorkflowRevision,
    WorkflowRevisionHash,
)
from atelier2.contracts.workflow_projections import (
    EnrichedPageBudget,
    WorkflowRevisionPage,
    WorkflowRevisionProjection,
)
from atelier2.ports.durable_runs import (
    AnyStartPublishedRunRequest,
    DurableAnswerActorMismatch,
    DurableAnswerCreated,
    DurableAnswerExisting,
    DurableAnswerNodeMissing,
    DurableAnswerNotAdmitted,
    DurableAnswerResult,
    DurableAnswerRevisionConflict,
    DurableAnswerRoundAnswered,
    DurableAnswerRunMissing,
    DurableAnswerStale,
    DurableAnswerStateConflict,
    DurablePublishedRunResult,
    DurableRunCreated,
    DurableRunExisting,
    DurableRunFormatNotExecutable,
    DurableRunIdentityConflict,
    DurableRunRevisionMissing,
    DurableStateCorrupt,
    DurableWriteUnavailable,
)
from atelier2.ports.effects import (
    DurableReconciliationCommandConflict,
    DurableReconciliationCreated,
    DurableReconciliationDeterminationConflict,
    DurableReconciliationExisting,
    DurableReconciliationResult,
)
from atelier2.ports.published_revisions import (
    CatalogNameFound,
    PublishedRevisionCollision,
    PublishedRevisionCreated,
    PublishedRevisionExisting,
    PublishedRevisionFound,
    PublishedRevisionMissing,
    PublishRevisionResult,
    ResolvePublishedRevisionResult,
)
from atelier2.ports.run_events import (
    CursorAhead,
    EventHistoryCorrupt,
    PrepareRunEventStreamResult,
    ReadRunEventPageResult,
    StreamReady,
)
from atelier2.ports.run_queries import (
    GetReconciliationRetryTargetResult,
    GetRunResult,
    ListRunsResult,
    ReconciliationRetryCommandConflict,
    ReconciliationRetryTargetMissing,
    RunFound,
    RunQueryMissing,
)
from atelier2.ports.workflow_revisions import (
    DurableProjectionLimit,
    DurableRevisionCollision,
    DurableRevisionCreated,
    DurableRevisionExisting,
    DurableRevisionPublicationResult,
    GetWorkflowRevisionResult,
    ListDescribedWorkflowRevisionsResult,
    ListWorkflowRevisionsResult,
    ProjectionTooLarge,
    QueryDurableStateCorrupt,
    ReadUnavailable,
    WorkflowRevisionFound,
    WorkflowRevisionMissing,
)
from tests.scenarios.api import (
    api_limits,
    api_ports,
    event_poll_backoff,
    unused_attention_event_page,
)
from tests.scenarios.workflows import ANY_JSON_SCHEMA, declared_output

AGENT_NODE_ID = "implement"
WAIT_NODE_ID = "wait"
DOCUMENT = (
    f"""format_version: 3
name: Implement a candidate, then ask whether it stands
nodes:
  - id: {AGENT_NODE_ID}
    type: agent
    role: builder
    mode: headless
    instruction: Do the one thing this chain is for.
""".encode()
    + declared_output()
    + f"""  - id: {WAIT_NODE_ID}
    type: wait
    prompt: Does this candidate stand?
    depends_on: [{AGENT_NODE_ID}]
""".encode()
    + declared_output(name="decision")
)
"""The document every route in this matrix speaks about.

The matrix is about which application result becomes which status and body, not
about any document in particular -- so this is the smallest executable line that
still names each node a request here addresses: the agent the node-detail route
reads, and the wait the answer route answers.
"""
REVISION = WorkflowRevision(DOCUMENT)
SCHEMA_DOCUMENT = b'{"type": "object"}'
SCHEMA_REVISION = PublishedRevision(RevisionKind.SCHEMA, SCHEMA_DOCUMENT)
BUDGET_DOCUMENT = b'{"attempt_deadline_seconds": 900}'
BUDGET_REVISION = PublishedRevision(RevisionKind.BUDGET_POLICY, BUDGET_DOCUMENT)
TOOL_GRANT_DOCUMENT = b'{"capability": "run-project-verification"}'
TOOL_GRANT_REVISION = PublishedRevision(RevisionKind.TOOL, TOOL_GRANT_DOCUMENT)
GRAPH = parse_executable_workflow_document(DOCUMENT)
REVISION_PROJECTION = WorkflowRevisionProjection(REVISION, GRAPH)
RUN = RunV3(
    RunId("run"),
    REVISION.revision_hash,
    AgentBindingSet(()).binding_set_hash,
    (),
    RunState.STARTED,
    AGENT_NODE_ID,
    0,
    0,
    RunConfigurationRevisionHash("c" * 64),
)
RUN_PROJECTION = RunProjection(RUN, GRAPH, None)
ANSWER = WaitAnswer(
    RUN.run_id,
    REVISION.revision_hash,
    WAIT_NODE_ID,
    NodeExecutionId.for_node(RUN.run_id, REVISION.revision_hash, WAIT_NODE_ID),
    WaitAnswerActor.OPERATOR,
    b"3",
)
PENDING_ANSWER = WaitAnswerSnapshot(ANSWER, WaitAnswerState.PENDING, 0)
APPLIED_ANSWER = WaitAnswerSnapshot(ANSWER, WaitAnswerState.APPLIED, 1)
INTENT = EffectIntent(
    EffectBinding(
        LogicalEffectKey("matrix-effect"),
        RUN.run_id,
        REVISION.revision_hash,
        AdapterRevision("matrix-adapter"),
        EffectDestination("matrix-destination"),
        AdapterOperationalIdentity("matrix-operation"),
    ),
    CanonicalRequest(b"request"),
)
INTENT_SNAPSHOT = EffectIntentSnapshot(
    INTENT,
    EffectIntentState.WAITING_RECONCILIATION,
    EffectIntentStateVersion(1),
)
COMMAND = ReconcileCommand(
    ReconcileCommandId("command"),
    INTENT.reference,
    EffectIntentStateVersion(1),
    ReconcileActor("operator"),
    "inspected",
    OperatorAuthoritativeAbsence(),
)
PENDING_COMMAND = ReconcileCommandSnapshot(COMMAND, ReconcileCommandState.PENDING)
APPLIED_COMMAND = ReconcileCommandSnapshot(COMMAND, ReconcileCommandState.APPLIED)
REJECTED_COMMAND = ReconcileCommandSnapshot(
    COMMAND, ReconcileCommandState.REJECTED_CONFLICT
)
RECONCILIATION_PROJECTION = RunProjection(
    RUN,
    GRAPH,
    WaitingReconciliationProjection(INTENT_SNAPSHOT, None),
)
RUN_BODY = {
    "workflow_format_version": 3,
    "run_id": "run",
    "public_run_reference": "run1.cnVu",
    "workflow_revision_hash": hashlib.sha256(DOCUMENT).hexdigest(),
    "workflow_name": "Implement a candidate, then ask whether it stands",
    "agent_binding_set_hash": RUN.binding_set_hash.value,
    "run_configuration_revision_hash": RUN.run_configuration_revision_hash.value,
    "agent_bindings": [],
    "orders": [],
    "work_item_reference": None,
    "fork_origin": None,
    "fork_successors": [],
    "answer": None,
    "refusal_output": None,
    "state_version": 0,
    "state": "STARTED",
    "current_node_id": AGENT_NODE_ID,
    "current_node_execution_id": NodeExecutionId.for_node(
        RUN.run_id, REVISION.revision_hash, AGENT_NODE_ID
    ).value,
    "node_rail": [
        {"node_id": AGENT_NODE_ID, "state": "working", "attempt": None},
        {"node_id": WAIT_NODE_ID, "state": "queued", "attempt": None},
    ],
    "cancellation": {
        "cancellable": False,
        "reason": "between-nodes",
        "target_node_execution_id": None,
    },
    "terminal_hash": None,
    "latest_event_cursor": None,
    "started_at": None,
    "ended_at": None,
}
"""The run every route that answers with a run answers with here.

`cancellation` refuses because this snapshot stands on an agent node with no
live attempt behind it -- the matrix scripts results, not attempts, so there is
nothing for a cancel to stop.
"""


@dataclass(frozen=True)
class RouteResultCase:
    identifier: str
    operation: str
    source: str
    result: object | None
    status: int
    problem_code: str | None = None


SUCCESS_CASES = (
    ("publish-created", "publish", "publisher", DurableRevisionCreated(REVISION), 201),
    (
        "publish-existing",
        "publish",
        "publisher",
        DurableRevisionExisting(REVISION),
        200,
    ),
    (
        "publish-schema-created",
        "publish-schema",
        "schema-registry",
        PublishedRevisionCreated(SCHEMA_REVISION),
        201,
    ),
    (
        "publish-schema-existing",
        "publish-schema",
        "schema-registry",
        PublishedRevisionExisting(SCHEMA_REVISION),
        200,
    ),
    (
        "publish-budget-created",
        "publish-budget",
        "budget-registry",
        PublishedRevisionCreated(BUDGET_REVISION),
        201,
    ),
    (
        "publish-budget-existing",
        "publish-budget",
        "budget-registry",
        PublishedRevisionExisting(BUDGET_REVISION),
        200,
    ),
    (
        "publish-tool-grant-created",
        "publish-tool-grant",
        "tool-grant-registry",
        PublishedRevisionCreated(TOOL_GRANT_REVISION),
        201,
    ),
    (
        "publish-tool-grant-existing",
        "publish-tool-grant",
        "tool-grant-registry",
        PublishedRevisionExisting(TOOL_GRANT_REVISION),
        200,
    ),
    (
        "revision-list-page",
        "revision-list",
        "revision-list",
        WorkflowRevisionPage((), None),
        200,
    ),
    (
        "revision-get-found",
        "revision-get",
        "revision-get",
        WorkflowRevisionFound(REVISION_PROJECTION),
        200,
    ),
    ("start-created", "start", "starter", DurableRunCreated(RUN), 201),
    ("start-existing", "start", "starter", DurableRunExisting(RUN), 200),
    ("run-list-page", "run-list", "run-list", RunPage((), None), 200),
    ("run-get-found", "run-get", "run-get", RunFound(RUN_PROJECTION), 200),
    (
        "wait-created-pending",
        "wait",
        "answerer",
        DurableAnswerCreated(PENDING_ANSWER),
        202,
    ),
    (
        "wait-existing-pending",
        "wait",
        "answerer",
        DurableAnswerExisting(PENDING_ANSWER),
        202,
    ),
    (
        "reconcile-created-pending",
        "reconcile",
        "commander",
        DurableReconciliationCreated(PENDING_COMMAND),
        202,
    ),
    (
        "reconcile-existing-pending",
        "reconcile",
        "commander",
        DurableReconciliationExisting(PENDING_COMMAND),
        202,
    ),
    (
        "reconcile-existing-applied",
        "reconcile",
        "commander",
        DurableReconciliationExisting(APPLIED_COMMAND),
        200,
    ),
    ("events-ready", "events", "events", StreamReady(0, True, 0), 200),
)
PROBLEM_CASES = (
    (
        "publish-invalid",
        "publish",
        "invalid-publish",
        None,
        422,
        "invalid-workflow-document",
    ),
    (
        "publish-collision",
        "publish",
        "publisher",
        DurableRevisionCollision(),
        409,
        "revision-collision",
    ),
    (
        "publish-unavailable",
        "publish",
        "publisher",
        DurableWriteUnavailable(),
        503,
        "temporarily-unavailable",
    ),
    (
        "publish-corrupt",
        "publish",
        "publisher",
        DurableStateCorrupt(),
        500,
        "durable-state-corrupt",
    ),
    (
        "publish-schema-invalid",
        "publish-schema",
        "invalid-schema",
        None,
        422,
        "schema-document-not-json",
    ),
    (
        "publish-schema-collision",
        "publish-schema",
        "schema-registry",
        PublishedRevisionCollision(),
        409,
        "schema-revision-collision",
    ),
    (
        "publish-budget-invalid",
        "publish-budget",
        "invalid-budget",
        None,
        422,
        "budget-missing-attempt-deadline",
    ),
    (
        "publish-budget-collision",
        "publish-budget",
        "budget-registry",
        PublishedRevisionCollision(),
        409,
        "budget-revision-collision",
    ),
    (
        "publish-budget-unavailable",
        "publish-budget",
        "budget-registry",
        DurableWriteUnavailable(),
        503,
        "temporarily-unavailable",
    ),
    (
        "publish-budget-corrupt",
        "publish-budget",
        "budget-registry",
        DurableStateCorrupt(),
        500,
        "durable-state-corrupt",
    ),
    (
        "publish-schema-unavailable",
        "publish-schema",
        "schema-registry",
        DurableWriteUnavailable(),
        503,
        "temporarily-unavailable",
    ),
    (
        "publish-schema-corrupt",
        "publish-schema",
        "schema-registry",
        DurableStateCorrupt(),
        500,
        "durable-state-corrupt",
    ),
    (
        "publish-tool-grant-invalid",
        "publish-tool-grant",
        "invalid-tool-grant",
        None,
        422,
        "tool-missing-capability",
    ),
    (
        "publish-tool-grant-collision",
        "publish-tool-grant",
        "tool-grant-registry",
        PublishedRevisionCollision(),
        409,
        "tool-grant-revision-collision",
    ),
    (
        "publish-tool-grant-unavailable",
        "publish-tool-grant",
        "tool-grant-registry",
        DurableWriteUnavailable(),
        503,
        "temporarily-unavailable",
    ),
    (
        "publish-tool-grant-corrupt",
        "publish-tool-grant",
        "tool-grant-registry",
        DurableStateCorrupt(),
        500,
        "durable-state-corrupt",
    ),
    (
        "revision-list-unavailable",
        "revision-list",
        "revision-list",
        ReadUnavailable(),
        503,
        "temporarily-unavailable",
    ),
    (
        "revision-list-corrupt",
        "revision-list",
        "revision-list",
        QueryDurableStateCorrupt(),
        500,
        "durable-state-corrupt",
    ),
    (
        "revision-list-projection-limit",
        "revision-list",
        "revision-list",
        ProjectionTooLarge(),
        500,
        "durable-projection-unrepresentable",
    ),
    (
        "revision-described-projection-limit",
        "revision-described",
        "revision-described",
        ProjectionTooLarge(),
        500,
        "durable-projection-unrepresentable",
    ),
    (
        "revision-get-missing",
        "revision-get",
        "revision-get",
        WorkflowRevisionMissing(),
        404,
        "workflow-revision-not-found",
    ),
    (
        "revision-get-unavailable",
        "revision-get",
        "revision-get",
        ReadUnavailable(),
        503,
        "temporarily-unavailable",
    ),
    (
        "revision-get-corrupt",
        "revision-get",
        "revision-get",
        QueryDurableStateCorrupt(),
        500,
        "durable-state-corrupt",
    ),
    (
        "revision-get-projection-limit",
        "revision-get",
        "revision-get",
        ProjectionTooLarge(),
        500,
        "durable-projection-unrepresentable",
    ),
    (
        "lineage-found-projection-limit",
        "lineage-found",
        "lineage-found",
        ProjectionTooLarge(),
        500,
        "durable-projection-unrepresentable",
    ),
    (
        "lineage-member-projection-limit",
        "lineage-member",
        "lineage-member",
        ProjectionTooLarge(),
        500,
        "durable-projection-unrepresentable",
    ),
    (
        "start-revision-missing",
        "start",
        "starter",
        DurableRunRevisionMissing(),
        404,
        "workflow-revision-not-found",
    ),
    (
        "start-identity-conflict",
        "start",
        "starter",
        DurableRunIdentityConflict(),
        409,
        "run-identity-conflict",
    ),
    (
        "start-format-not-executable",
        "start",
        "starter",
        DurableRunFormatNotExecutable(),
        409,
        "workflow-format-not-executable",
    ),
    (
        "start-unavailable",
        "start",
        "starter",
        DurableWriteUnavailable(),
        503,
        "temporarily-unavailable",
    ),
    (
        "start-corrupt",
        "start",
        "starter",
        DurableStateCorrupt(),
        500,
        "durable-state-corrupt",
    ),
    (
        "run-list-unavailable",
        "run-list",
        "run-list",
        ReadUnavailable(),
        503,
        "temporarily-unavailable",
    ),
    (
        "run-list-corrupt",
        "run-list",
        "run-list",
        QueryDurableStateCorrupt(),
        500,
        "durable-state-corrupt",
    ),
    (
        "run-list-projection-limit",
        "run-list",
        "run-list",
        ProjectionTooLarge(),
        500,
        "durable-projection-unrepresentable",
    ),
    ("run-get-missing", "run-get", "run-get", RunQueryMissing(), 404, "run-not-found"),
    (
        "run-get-unavailable",
        "run-get",
        "run-get",
        ReadUnavailable(),
        503,
        "temporarily-unavailable",
    ),
    (
        "run-get-corrupt",
        "run-get",
        "run-get",
        QueryDurableStateCorrupt(),
        500,
        "durable-state-corrupt",
    ),
    (
        "run-get-projection-limit",
        "run-get",
        "run-get",
        ProjectionTooLarge(),
        500,
        "durable-projection-unrepresentable",
    ),
    (
        "wait-existing-applied",
        "wait",
        "answerer",
        DurableAnswerExisting(APPLIED_ANSWER),
        200,
        None,
    ),
    (
        "wait-unanswerable",
        "wait",
        "answerer",
        DurableAnswerNotAdmitted("the waiting node does not admit these bytes"),
        422,
        "invalid-request",
    ),
    (
        "wait-run-missing",
        "wait",
        "answerer",
        DurableAnswerRunMissing(),
        404,
        "run-not-found",
    ),
    (
        "wait-node-missing",
        "wait",
        "answerer",
        DurableAnswerNodeMissing(),
        404,
        "node-not-found",
    ),
    (
        "wait-revision-conflict",
        "wait",
        "answerer",
        DurableAnswerRevisionConflict(),
        409,
        "answer-revision-conflict",
    ),
    (
        "wait-state-conflict",
        "wait",
        "answerer",
        DurableAnswerStateConflict(),
        409,
        "answer-state-conflict",
    ),
    (
        "wait-round-answered",
        "wait",
        "answerer",
        DurableAnswerRoundAnswered(),
        409,
        "answer-state-conflict",
    ),
    (
        "wait-execution-stale",
        "wait",
        "answerer",
        DurableAnswerStale(),
        409,
        "answer-execution-stale",
    ),
    (
        "wait-actor-mismatch",
        "wait",
        "answerer",
        DurableAnswerActorMismatch(WaitAnswerActor.OPERATOR),
        422,
        "invalid-request",
    ),
    (
        "wait-unavailable",
        "wait",
        "answerer",
        DurableWriteUnavailable(),
        503,
        "temporarily-unavailable",
    ),
    (
        "wait-corrupt",
        "wait",
        "answerer",
        DurableStateCorrupt(),
        500,
        "durable-state-corrupt",
    ),
    (
        "reconcile-run-missing",
        "reconcile",
        "reconcile-run",
        RunQueryMissing(),
        404,
        "run-not-found",
    ),
    (
        "reconcile-target-missing",
        "reconcile",
        "reconcile-retry",
        ReconciliationRetryTargetMissing(),
        409,
        "reconciliation-target-missing",
    ),
    (
        "reconcile-retry-command-conflict",
        "reconcile",
        "reconcile-retry",
        ReconciliationRetryCommandConflict(),
        409,
        "reconciliation-command-conflict",
    ),
    (
        "reconcile-retry-run-missing",
        "reconcile",
        "reconcile-retry",
        RunQueryMissing(),
        404,
        "run-not-found",
    ),
    (
        "reconcile-retry-unavailable",
        "reconcile",
        "reconcile-retry",
        ReadUnavailable(),
        503,
        "temporarily-unavailable",
    ),
    (
        "reconcile-retry-corrupt",
        "reconcile",
        "reconcile-retry",
        QueryDurableStateCorrupt(),
        500,
        "durable-state-corrupt",
    ),
    (
        "reconcile-stale",
        "reconcile",
        "commander",
        DurableReconciliationCreated(REJECTED_COMMAND),
        409,
        "reconciliation-stale",
    ),
    (
        "reconcile-command-conflict",
        "reconcile",
        "commander",
        DurableReconciliationCommandConflict(),
        409,
        "reconciliation-command-conflict",
    ),
    (
        "reconcile-determination-conflict",
        "reconcile",
        "commander",
        DurableReconciliationDeterminationConflict(),
        409,
        "reconciliation-determination-conflict",
    ),
    (
        "reconcile-existing-rejected",
        "reconcile",
        "commander",
        DurableReconciliationExisting(REJECTED_COMMAND),
        409,
        "reconciliation-rejected",
    ),
    (
        "reconcile-unavailable",
        "reconcile",
        "commander",
        DurableWriteUnavailable(),
        503,
        "temporarily-unavailable",
    ),
    (
        "reconcile-corrupt",
        "reconcile",
        "commander",
        DurableStateCorrupt(),
        500,
        "durable-state-corrupt",
    ),
    (
        "reconcile-projection-limit",
        "reconcile",
        "reconcile-retry",
        ProjectionTooLarge(),
        500,
        "durable-projection-unrepresentable",
    ),
    ("events-run-missing", "events", "events", RunQueryMissing(), 404, "run-not-found"),
    (
        "events-cursor-ahead",
        "events",
        "events",
        CursorAhead(),
        409,
        "event-cursor-ahead",
    ),
    (
        "events-history-corrupt",
        "events",
        "events",
        EventHistoryCorrupt(),
        500,
        "durable-state-corrupt",
    ),
    (
        "events-query-corrupt",
        "events",
        "events",
        QueryDurableStateCorrupt(),
        500,
        "durable-state-corrupt",
    ),
    (
        "events-unavailable",
        "events",
        "events",
        ReadUnavailable(),
        503,
        "temporarily-unavailable",
    ),
    (
        "events-run-projection-limit",
        "events",
        "run-get",
        ProjectionTooLarge(),
        500,
        "durable-projection-unrepresentable",
    ),
    (
        "node-detail-projection-limit",
        "node-detail",
        "node-detail",
        ProjectionTooLarge(),
        500,
        "durable-projection-unrepresentable",
    ),
    (
        "attention-resume-projection-limit",
        "attention",
        "attention",
        ProjectionTooLarge(),
        500,
        "durable-projection-unrepresentable",
    ),
)
CASES = tuple(RouteResultCase(*values) for values in SUCCESS_CASES) + tuple(
    RouteResultCase(*values) for values in PROBLEM_CASES
)


@dataclass
class MatrixPublisher:
    case: RouteResultCase

    def publish(self, revision: WorkflowRevision) -> DurableRevisionPublicationResult:
        del revision
        if self.case.source == "invalid-publish":
            raise AssertionError("invalid workflow reached the publisher")
        assert self.case.source == "publisher"
        return cast(DurableRevisionPublicationResult, self.case.result)


@dataclass
class MatrixRegistry:
    case: RouteResultCase

    def publish_revision(self, revision: PublishedRevision) -> PublishRevisionResult:
        del revision
        if self.case.source in {
            "invalid-schema",
            "invalid-budget",
            "invalid-tool-grant",
        }:
            raise AssertionError("an invalid document reached the registry")
        assert self.case.source in {
            "schema-registry",
            "budget-registry",
            "tool-grant-registry",
        }
        return cast(PublishRevisionResult, self.case.result)

    def resolve(
        self, kind: RevisionKind, revision_hash: PublishedRevisionHash
    ) -> ResolvePublishedRevisionResult:
        """The one schema this matrix's document pins; nothing else is published.

        Every read of the document resolves its declared outputs before it can
        say whether the revision is executable, so the registry has to answer
        that reference for real -- otherwise the whole matrix would read back a
        document refused for a reference the scenario simply never published.
        """
        if (
            kind is RevisionKind.SCHEMA
            and revision_hash == ANY_JSON_SCHEMA.revision_hash
        ):
            return PublishedRevisionFound(ANY_JSON_SCHEMA)
        return PublishedRevisionMissing()

    @contextmanager
    def resolver_session(self) -> Iterator[MatrixRegistry]:
        """The matrix scenario resolves one document's own references, never
        many pages -- one connection's worth of state is this object itself."""
        yield self


@dataclass
class MatrixCatalog:
    case: RouteResultCase

    def resolve(self, kind: object, revision_hash: object) -> object:
        del kind, revision_hash
        assert self.case.source in {"lineage-found", "lineage-member"}
        return PublishedRevisionMissing()

    def resolve_name(self, kind: object, query: object, position: object) -> object:
        del kind, query, position
        assert self.case.source == "lineage-member"
        return CatalogNameFound(
            CatalogLineageId(REVISION.revision_hash.value),
            PublishedRevisionHash(REVISION.revision_hash.value),
            1,
            CatalogLineageDisplayName("lineage"),
            False,
        )


@dataclass
class MatrixStarter:
    case: RouteResultCase

    def start_published(
        self, request: AnyStartPublishedRunRequest
    ) -> DurablePublishedRunResult:
        del request
        assert self.case.source == "starter"
        return cast(DurablePublishedRunResult, self.case.result)


@dataclass
class MatrixAnswerer:
    case: RouteResultCase

    def submit_result(self, request: SubmitWaitAnswerRequest) -> DurableAnswerResult:
        del request
        assert self.case.source == "answerer"
        return cast(DurableAnswerResult, self.case.result)


@dataclass
class MatrixCommander:
    case: RouteResultCase

    def submit_result(self, command: ReconcileCommand) -> DurableReconciliationResult:
        del command
        assert self.case.source == "commander"
        return cast(DurableReconciliationResult, self.case.result)


@dataclass
class MatrixQueries:
    case: RouteResultCase
    run_reads: int = 0

    def list_workflow_revisions(
        self, after: WorkflowRevisionHash | None, limit: int
    ) -> ListWorkflowRevisionsResult:
        del after, limit
        assert self.case.source == "revision-list"
        return cast(ListWorkflowRevisionsResult, self.case.result)

    def list_described_workflow_revisions(
        self,
        after: WorkflowRevisionHash | None,
        limit: int,
        budget: EnrichedPageBudget,
    ) -> ListDescribedWorkflowRevisionsResult:
        del after, limit, budget
        assert self.case.source == "revision-described"
        return cast(ListDescribedWorkflowRevisionsResult, self.case.result)

    def get_workflow_revision(
        self,
        revision_hash: WorkflowRevisionHash,
        projection_limit: DurableProjectionLimit | None = None,
    ) -> GetWorkflowRevisionResult:
        del revision_hash, projection_limit
        if self.case.operation == "start":
            # The start route asks what format it would have to answer with
            # before it writes anything; every matrix start case is a document
            # the API can serve, so the read falls through to the starter.
            return WorkflowRevisionFound(REVISION_PROJECTION)
        if self.case.source in {"lineage-found", "lineage-member"}:
            return ProjectionTooLarge()
        assert self.case.source == "revision-get"
        return cast(GetWorkflowRevisionResult, self.case.result)

    def list_runs(
        self,
        after: RunId | None,
        limit: int,
        state: object = None,
        projection_limit: DurableProjectionLimit | None = None,
    ) -> ListRunsResult:
        del after, limit, state, projection_limit
        assert self.case.source == "run-list"
        return cast(ListRunsResult, self.case.result)

    def get_run(
        self,
        run_id: RunId,
        projection_limit: DurableProjectionLimit | None = None,
    ) -> GetRunResult:
        del run_id, projection_limit
        self.run_reads += 1
        if self.case.source == "run-get":
            return cast(GetRunResult, self.case.result)
        if self.case.source == "reconcile-run":
            return cast(GetRunResult, self.case.result)
        if self.case.source == "reconcile-retry":
            return RunFound(RUN_PROJECTION)
        if self.case.source == "events":
            return RunFound(RUN_PROJECTION)
        if self.case.source == "commander" and self.run_reads == 1:
            return RunFound(RECONCILIATION_PROJECTION)
        if self.case.source in {"starter", "answerer", "commander"}:
            return RunFound(RUN_PROJECTION)
        raise AssertionError("matrix route unexpectedly read a run")

    def get_node_detail(self, run_id: RunId, node_id: str) -> object:
        del run_id, node_id
        assert self.case.source == "node-detail"
        return self.case.result

    def get_reconciliation_retry_target(
        self,
        run_id: RunId,
        command_id: ReconcileCommandId,
        projection_limit: DurableProjectionLimit | None = None,
    ) -> GetReconciliationRetryTargetResult:
        del run_id, command_id, projection_limit
        assert self.case.source == "reconcile-retry"
        return cast(GetReconciliationRetryTargetResult, self.case.result)

    def prepare_run_event_stream(
        self, run_id: RunId, after_sequence: int
    ) -> PrepareRunEventStreamResult:
        del run_id, after_sequence
        if self.case.source == "run-get":
            return StreamReady(0, True, 0)
        assert self.case.source == "events"
        return cast(PrepareRunEventStreamResult, self.case.result)

    def read_run_event_page(
        self,
        run_id: RunId,
        after_sequence: int,
        limit: int,
        projection_limit: DurableProjectionLimit | None = None,
    ) -> ReadRunEventPageResult:
        del run_id, after_sequence, limit, projection_limit
        raise AssertionError("terminal matrix stream unexpectedly paged events")

    def read_attention_event_page(
        self,
        after_run_id: RunId | None,
        after_sequence: int | None,
        limit: int,
        excluded_identities: tuple[tuple[RunId, int], ...] = (),
    ) -> object:
        if self.case.source == "attention":
            return self.case.result
        return unused_attention_event_page(
            after_run_id, after_sequence, limit, excluded_identities
        )


def _ports(case: RouteResultCase) -> ApiPorts:
    queries = MatrixQueries(case)
    return api_ports(
        workflow_revision_publisher=MatrixPublisher(case),
        published_run_starter=MatrixStarter(case),
        wait_answerer=MatrixAnswerer(case),
        reconcile_commander=MatrixCommander(case),
        workflow_revision_queries=queries,
        run_queries=queries,
        run_event_queries=queries,
        published_revision_registry=MatrixRegistry(case),
        published_revision_resolver_sessions=MatrixRegistry(case),
        catalog_resolver=MatrixCatalog(case),
        workflow_document_parser=parse_executable_workflow_document,
        agent_definition_parser=parse_agent_definition,
        agent_definition_renderer=render_agent_definition,
        agent_configuration_catalog=queries,
    )


def _catalog_admission_body() -> dict[str, str]:
    """One admission body, spelled as the kind-generic door reads it."""

    return {
        "kind": RevisionKind.WORKFLOW.value,
        "catalog_revision_hash": REVISION.revision_hash.value,
        "actor": "operator",
        "activated_at": "2026-08-27T12:00:00Z",
    }


def _request(client: TestClient, case: RouteResultCase):
    if case.operation == "publish":
        document = b"not a workflow" if case.source == "invalid-publish" else DOCUMENT
        return client.post(
            "/atelier/api/v1/workflow-revisions",
            content=document,
            headers={"content-type": "application/yaml"},
        )
    if case.operation == "publish-schema":
        document = (
            b"Guten Morgen" if case.source == "invalid-schema" else SCHEMA_DOCUMENT
        )
        return client.post(
            "/atelier/api/v1/schema-revisions",
            content=document,
            headers={"content-type": "application/json"},
        )
    if case.operation == "publish-budget":
        document = (
            b'{"maximum_assistant_turns": 8}'
            if case.source == "invalid-budget"
            else BUDGET_DOCUMENT
        )
        return client.post(
            "/atelier/api/v1/budget-revisions",
            content=document,
            headers={"content-type": "application/json"},
        )
    if case.operation == "publish-tool-grant":
        document = b"{}" if case.source == "invalid-tool-grant" else TOOL_GRANT_DOCUMENT
        return client.post(
            "/atelier/api/v1/tool-grant-revisions",
            content=document,
            headers={"content-type": "application/json"},
        )
    if case.operation == "revision-list":
        return client.get("/atelier/api/v1/workflow-revisions")
    if case.operation == "revision-described":
        return client.get("/atelier/api/v1/workflow-revisions?view=described")
    if case.operation == "revision-get":
        return client.get(
            "/atelier/api/v1/workflow-revisions/" + REVISION.revision_hash.value
        )
    if case.operation == "lineage-found":
        return client.post(CATALOG_LINEAGES_PATH, json=_catalog_admission_body())
    if case.operation == "lineage-member":
        return client.post(
            f"{CATALOG_LINEAGES_PATH}/{REVISION.revision_hash.value}/members",
            json=_catalog_admission_body(),
        )
    if case.operation == "start":
        return client.post(
            "/atelier/api/v1/runs",
            json={
                "run_id": "run",
                "workflow_revision_hash": REVISION.revision_hash.value,
            },
        )
    if case.operation == "run-list":
        return client.get("/atelier/api/v1/runs")
    if case.operation == "run-get":
        return client.get("/atelier/api/v1/runs/run1.cnVu")
    if case.operation == "wait":
        return client.post(
            "/atelier/api/v1/runs/run1.cnVu/answers",
            json={
                "workflow_revision_hash": REVISION.revision_hash.value,
                "node_id": WAIT_NODE_ID,
                "expected_node_execution_id": ANSWER.node_execution_id.value,
                "actor": "operator",
                "answer_base64": "Mw==",
            },
        )
    if case.operation == "reconcile":
        return client.post(
            "/atelier/api/v1/runs/run1.cnVu/reconciliations",
            json={
                "command_id": "command",
                "expected_intent_state_version": 1,
                "actor": "operator",
                "evidence": "inspected",
                "determination": {"type": "operator_authoritative_absence"},
            },
        )
    if case.operation == "events":
        return client.get(
            "/atelier/api/v1/runs/run1.cnVu/events",
            headers={"accept": "text/event-stream"},
        )
    if case.operation == "node-detail":
        return client.get(f"/atelier/api/v1/runs/run1.cnVu/nodes/{AGENT_NODE_ID}")
    if case.operation == "attention":
        return client.get(
            "/atelier/api/v1/events",
            headers={"accept": "text/event-stream", "last-event-id": "event1.cnVu.1"},
        )
    raise AssertionError(f"matrix has no request for {case.operation}")


def _success_body(operation: str) -> object:
    schema_revision = ANY_JSON_SCHEMA.revision_hash.value
    revision_body = {
        "workflow_revision_hash": REVISION.revision_hash.value,
        "document_base64": base64.b64encode(DOCUMENT).decode("ascii"),
        "graph": {
            "workflow_format_version": 3,
            "executable": True,
            "not_executable_reason": None,
            "node_count": 2,
            "agent_roles": ["builder"],
            "orders": [],
            "wait_answer_schemas": [
                {
                    "node_id": WAIT_NODE_ID,
                    "schema": {
                        "ref": "decision-schema",
                        "revision": schema_revision,
                    },
                    "kind": "free",
                    "string_typed": False,
                    "values": None,
                }
            ],
            "node_previews": [
                {
                    "id": AGENT_NODE_ID,
                    "kind": "agent",
                    "role": "builder",
                    "instruction_start": "Do the one thing this chain is for.",
                    "depends_on": [],
                },
                {
                    "id": WAIT_NODE_ID,
                    "kind": "wait",
                    "role": None,
                    "instruction_start": None,
                    "depends_on": [AGENT_NODE_ID],
                },
            ],
            "loops": [],
            "name": "Implement a candidate, then ask whether it stands",
            "description": None,
        },
        "provenance": None,
    }
    if operation == "publish-schema":
        return {"schema_revision_hash": SCHEMA_REVISION.revision_hash.value}
    if operation == "publish-budget":
        return {"budget_revision_hash": BUDGET_REVISION.revision_hash.value}
    if operation == "publish-tool-grant":
        return {"tool_grant_revision_hash": TOOL_GRANT_REVISION.revision_hash.value}
    if operation in {"publish", "revision-get"}:
        return revision_body
    if operation == "revision-list":
        return {"items": [], "next_after_revision_hash": None}
    if operation in {"start", "run-get", "wait", "reconcile"}:
        return RUN_BODY
    if operation == "run-list":
        return {"items": [], "next_after": None}
    if operation == "events":
        return b""
    raise AssertionError(f"matrix has no body oracle for {operation}")


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.identifier)
def test_every_application_result_branch_has_exact_http_mapping(
    case: RouteResultCase,
) -> None:
    client = TestClient(
        create_app(
            source_commit="commit",
            source_tree="tree",
            ports=_ports(case),
            limits=api_limits(),
            event_poll_backoff=event_poll_backoff(),
        ),
        raise_server_exceptions=False,
    )

    response = _request(client, case)

    assert response.status_code == case.status
    if case.problem_code is not None:
        assert response.headers["content-type"] == "application/problem+json"
        assert response.json()["type"] == (
            "urn:atelier2:problem:v1:" + case.problem_code
        )
        return
    if case.operation == "events":
        assert response.headers["content-type"] == "text/event-stream; charset=utf-8"
        assert response.content == _success_body(case.operation)
    else:
        assert response.headers["content-type"] == "application/json"
        assert response.json() == _success_body(case.operation)


@pytest.mark.proves("a-port-refuses-by-type-and-the-api-words-the-answer")
def test_a_typed_projection_refusal_tells_the_operator_what_to_do() -> None:
    case = RouteResultCase(
        "reconcile-retry-projection-limit",
        "reconcile",
        "reconcile-retry",
        ProjectionTooLarge(),
        500,
        "durable-projection-unrepresentable",
    )
    client = TestClient(
        create_app(
            source_commit="commit",
            source_tree="tree",
            ports=_ports(case),
            limits=api_limits(),
            event_poll_backoff=event_poll_backoff(),
        )
    )

    response = _request(client, case)

    assert response.status_code == 500
    assert (
        response.json()["detail"] == "Inspect the durable projection before retrying."
    )


# T16 — the four request-side branches the #11 audit named and no test held.
# They never reach a port: what refuses them is the wire, so they are driven
# here beside the result matrix rather than through a scripted outcome, and the
# ports below assert they were not consulted at all.


def _unreached_ports() -> ApiPorts:
    """A composition whose every port fails the test if a route reaches it."""
    return _ports(
        RouteResultCase("t16-unreached", "wait", "answerer", UnreachedAnswer(), 500)
    )


@dataclass(frozen=True)
class UnreachedAnswer:
    """Handed to the scripted answerer so a reach is a failure, not a 500."""


@pytest.mark.proves("a-port-refuses-by-type-and-the-api-words-the-answer")
@pytest.mark.parametrize(
    ("identifier", "path", "body", "headers", "status", "problem_code"),
    [
        pytest.param(
            "non-canonical-integer",
            "/atelier/api/v1/runs/run1.cnVu/reconciliations",
            {
                "command_id": "command",
                "expected_intent_state_version": "1",
                "actor": "operator",
                "evidence": "inspected",
                "determination": {"type": "operator_authoritative_absence"},
            },
            None,
            422,
            "invalid-request",
            id="a version written as text is not a version",
        ),
        pytest.param(
            "invalid-base64",
            "/atelier/api/v1/runs/run1.cnVu/answers",
            {
                "workflow_revision_hash": REVISION.revision_hash.value,
                "node_id": WAIT_NODE_ID,
                "expected_node_execution_id": ANSWER.node_execution_id.value,
                "actor": "operator",
                "answer_base64": "not base64!!",
            },
            None,
            422,
            # Not the generic refusal: the API names the field that is wrong,
            # which is the more useful answer and the one it actually gives.
            "invalid-base64",
            id="an answer that is not base64 is refused before the store",
        ),
        pytest.param(
            "unsupported-media-type",
            "/atelier/api/v1/runs/run1.cnVu/answers",
            {
                "workflow_revision_hash": REVISION.revision_hash.value,
                "node_id": WAIT_NODE_ID,
                "expected_node_execution_id": ANSWER.node_execution_id.value,
                "actor": "operator",
                "answer_base64": "Mw==",
            },
            {"content-type": "text/plain"},
            415,
            "unsupported-media-type",
            id="a body the route does not read is refused by its media type",
        ),
        pytest.param(
            "two-rules-one-answer",
            "/atelier/api/v1/runs/run1.cnVu/answers",
            {
                "workflow_revision_hash": "not-a-hash",
                "node_id": WAIT_NODE_ID,
                "expected_node_execution_id": ANSWER.node_execution_id.value,
                "actor": "operator",
                "answer_base64": "not base64!!",
            },
            None,
            422,
            "invalid-request",
            id="a request that breaks two rules still answers exactly one code",
        ),
    ],
)
def test_a_malformed_request_is_refused_by_name_without_reaching_a_port(
    identifier: str,
    path: str,
    body: dict[str, object],
    headers: dict[str, str] | None,
    status: int,
    problem_code: str,
) -> None:
    """What the wire refuses never becomes a question for the store.

    Each of these was named by the #11 audit as a branch no test held. They are
    one class: the request is wrong in a way the API can see for itself, so the
    answer must be exact and the durable side must stay untouched -- a malformed
    request that reached a port would be a write attempted on a value nobody
    validated.
    """
    ports = _unreached_ports()
    client = TestClient(
        create_app(
            source_commit="commit",
            source_tree="tree",
            ports=ports,
            limits=api_limits(),
            event_poll_backoff=event_poll_backoff(),
        )
    )

    response = client.post(path, json=body, headers=headers)

    assert response.status_code == status
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["type"] == "urn:atelier2:problem:v1:" + problem_code
