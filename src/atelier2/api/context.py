from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from fastapi import Depends, FastAPI, Request

from atelier2.api.limits import ApiLimits
from atelier2.api.seat import SeatReader
from atelier2.api.stream import BoundedQueryRunner, EventPollBackoff
from atelier2.application.admit_catalog_member import (
    AdmitMemberResult,
    FoundLineageResult,
)
from atelier2.application.admit_library_addition import (
    AdmitLibraryAdditionResult,
    ReadLibraryAdditionResult,
)
from atelier2.application.admit_queue_item import (
    ConfirmQueueProposalOutcome,
    ListQueueItemsOutcome,
)
from atelier2.application.answer_wait import AnswerWaitResult
from atelier2.application.cancel_agent_attempt import CancelAgentAttemptResult
from atelier2.application.cancel_run import CancelRunResult
from atelier2.application.classify_definition_document import (
    ClassifyDefinitionDocumentResult,
)
from atelier2.application.fork_run import ForkRunResult
from atelier2.application.import_project_source_issues import (
    ImportProjectSourceIssuesOutcome,
)
from atelier2.application.model_configuration import (
    GetModelRegistryResult,
    GetProjectModelDefaultsResult,
    GetProjectModelResolutionResult,
    PublishModelRegistryUseCaseResult,
    PublishProjectModelDefaultsUseCaseResult,
)
from atelier2.application.plan_queue_item import (
    PlanQueueItemOutcome,
    PutQueueProjectPolicyOutcome,
)
from atelier2.application.prepare_run_events import PrepareRunEventsResult
from atelier2.application.project_connections import (
    ConnectManagedProjectSourceResult,
    DisconnectProjectSourceResult,
    GetServedProjectSourceConnectionResult,
    ListProjectSourcesResult,
    RotateProjectSourceTokenResult,
)
from atelier2.application.publish_adapter_operation_revision import (
    PublishAdapterOperationRevisionResult,
)
from atelier2.application.publish_agent_configurations import (
    PublishAgentConfigurationRevisionResult,
    PublishAuthProfileRevisionResult,
)
from atelier2.application.publish_agent_definition_revision import (
    PublishAgentDefinitionRevisionResult,
)
from atelier2.application.publish_artifact import PublishArtifactUseCaseResult
from atelier2.application.publish_budget_revision import PublishBudgetRevisionResult
from atelier2.application.publish_schema_revision import (
    GetSchemaRevisionResult,
    PublishSchemaRevisionResult,
)
from atelier2.application.publish_tool_grant_revision import (
    PublishToolGrantRevisionResult,
)
from atelier2.application.publish_workflow_revision import (
    PublishWorkflowRevisionResult,
    WorkflowPublicationLimits,
)
from atelier2.application.read_agent_configurations import (
    ListAgentConfigurationRevisionsResult,
    ListAuthProfileRevisionsResult,
)
from atelier2.application.read_agent_definition_revisions import (
    GetAgentDefinitionRevisionResult,
    ListAgentDefinitionRevisionsResult,
)
from atelier2.application.read_artifact import ReadArtifactResult
from atelier2.application.read_attention_events import ReadAttentionEventsResult
from atelier2.application.read_projects import (
    GetProjectResult,
    ListProjectsResult,
)
from atelier2.application.read_redeploy_status import ReadRedeployStatusResult
from atelier2.application.read_run_events import ReadRunEventsResult
from atelier2.application.read_runs import (
    GetNodeDetailUseCaseResult,
    GetRunResult,
    ListRunsResult,
)
from atelier2.application.read_workflow_revisions import (
    GetWorkflowRevisionResult,
    ListDescribedWorkflowRevisionsResult,
    ListWorkflowRevisionsResult,
)
from atelier2.application.reconcile_effect import ReconcileRunResult
from atelier2.application.reconcile_run import ReconcileRunRequest
from atelier2.application.reconstruct_agent_definition import (
    AgentDefinitionParser,
    AgentDefinitionRenderer,
)
from atelier2.application.resolve_catalog_name import CatalogNameResult
from atelier2.application.retire_catalog_lineage import (
    RetireCatalogLineageUseCaseResult,
)
from atelier2.application.start_published_run import (
    AuthoredAgentBinding,
    AuthoredOrder,
    StartPublishedRunResult,
)
from atelier2.contracts.agent_attempts import CancelAgentAttemptRequest
from atelier2.contracts.agents import (
    AgentConfigurationRevisionHash,
    AuthProfileRevisionHash,
)
from atelier2.contracts.artifacts import ArtifactHash
from atelier2.contracts.catalog_intakes import CatalogIntakeId, CatalogIntakeKind
from atelier2.contracts.catalog_v3 import (
    CatalogActivatedAt,
    CatalogActor,
    CatalogLineageDisplayName,
    CatalogLineageId,
    CatalogLineageQuery,
)
from atelier2.contracts.executions import NodeExecutionId, WaitAnswerActor
from atelier2.contracts.host_configuration import ProjectId, ProjectSourceId
from atelier2.contracts.queue_projection import (
    ConfirmQueueProposal,
    PlanQueueItem,
    QueueItemId,
    QueueProjectPolicyRevision,
)
from atelier2.contracts.revisions_v3 import PublishedRevisionHash, RevisionKind
from atelier2.contracts.runs import RunId, RunState, WorkflowRevisionHash
from atelier2.contracts.when import RecordedAt
from atelier2.ports.agent_attempts import TransactionalAgentAttemptCanceller
from atelier2.ports.agent_configurations import AgentConfigurationCatalog
from atelier2.ports.artifacts import ArtifactPublisher, ArtifactReader
from atelier2.ports.catalog_intakes import (
    CatalogIntakes,
)
from atelier2.ports.durable_runs import (
    DurablePublishedRunStarter,
    TransactionalWaitAnswerer,
)
from atelier2.ports.effects import TransactionalEffectReconcileCommander
from atelier2.ports.host_configuration import (
    HostConfigurationChannel,
    ProjectSourceConnectionChannel,
    ProviderModelDiscoverer,
    ProviderModelValidator,
)
from atelier2.ports.issue_observation import TrackerItemSource
from atelier2.ports.project_connections import (
    ManagedProjectSourceCredentialStore,
    ProjectSourceConnector,
)
from atelier2.ports.published_revisions import (
    CatalogAdmissions,
    CatalogResolver,
    LibraryAdditions,
    PublishedRevisionListing,
    PublishedRevisionRegistry,
    PublishedRevisionResolverWithSession,
)
from atelier2.ports.queue_projection import QueueProjection
from atelier2.ports.redeploy_status import RedeployStatusReader
from atelier2.ports.run_events import (
    RunEventQueries,
)
from atelier2.ports.run_queries import (
    RunQueries,
)
from atelier2.ports.workflow_revisions import (
    WorkflowDocumentParser,
    WorkflowRevisionPublisher,
    WorkflowRevisionQueries,
)


@dataclass(frozen=True)
class ApiPorts:
    workflow_revision_publisher: WorkflowRevisionPublisher
    published_run_starter: DurablePublishedRunStarter
    wait_answerer: TransactionalWaitAnswerer
    reconcile_commander: TransactionalEffectReconcileCommander
    workflow_revision_queries: WorkflowRevisionQueries
    run_queries: RunQueries
    run_event_queries: RunEventQueries
    workflow_document_parser: WorkflowDocumentParser
    agent_definition_parser: AgentDefinitionParser
    agent_definition_renderer: AgentDefinitionRenderer
    agent_configuration_catalog: AgentConfigurationCatalog
    agent_attempt_canceller: TransactionalAgentAttemptCanceller
    catalog_resolver: CatalogResolver
    catalog_admissions: CatalogAdmissions
    library_additions: LibraryAdditions
    catalog_intakes: CatalogIntakes
    published_revision_registry: PublishedRevisionRegistry
    # Distinct from `published_revision_registry`: the one composed read that
    # resolves many references per page (`list_described_workflow_revisions`)
    # needs a resolver that can open a session, which most single-lookup
    # callers of the registry above do not and should not have to carry
    # (#937). The two are wired to the same durable store in production.
    published_revision_resolver_sessions: PublishedRevisionResolverWithSession
    published_revision_listing: PublishedRevisionListing
    artifact_publisher: ArtifactPublisher
    artifact_reader: ArtifactReader
    host_configuration_channel: HostConfigurationChannel
    project_source_connection_channel: ProjectSourceConnectionChannel
    project_source_connector: ProjectSourceConnector
    project_source_credential_store: ManagedProjectSourceCredentialStore
    queue_projection: QueueProjection
    # None is the honest default: a composition that serves no connected
    # project has no tracker to observe, and the import door says so by name.
    tracker_item_source: TrackerItemSource | None = None
    # What an admission asks for the moment it commits, so a newly admitted
    # item does not wait out the queue sweep's tick before it starts. `None`
    # where no runtime clock stands behind this app -- a composition without
    # one says so rather than pretending an admission started anything.
    request_queue_sweep: Callable[[], None] | None = None
    model_registry_discoverer: ProviderModelDiscoverer | None = None
    model_registry_validator: ProviderModelValidator | None = None
    # None is the honest default too: a deployment with no auto-redeploy
    # watcher in front of it (every test app, and any host serving without
    # one) has no status file for GET /health to read.
    redeploy_status_reader: RedeployStatusReader | None = None


@dataclass(frozen=True)
class WorkflowRevisionUseCases:
    """Reading and publishing workflow revisions."""

    get_workflow_revision: Callable[[WorkflowRevisionHash], GetWorkflowRevisionResult]
    list_workflow_revisions: Callable[
        [WorkflowRevisionHash | None, int], ListWorkflowRevisionsResult
    ]
    list_described_workflow_revisions: Callable[
        [WorkflowRevisionHash | None, int], ListDescribedWorkflowRevisionsResult
    ]
    publish_workflow_revision: Callable[[bytes], PublishWorkflowRevisionResult]


@dataclass(frozen=True)
class RunUseCases:
    """Reading a run, its nodes, and its event stream."""

    get_run: Callable[[RunId], GetRunResult]
    get_node_detail: Callable[[RunId, str], GetNodeDetailUseCaseResult]
    list_runs: Callable[[RunId | None, int, RunState | None], ListRunsResult]
    prepare_run_events: Callable[[RunId, int], PrepareRunEventsResult]
    read_run_events: Callable[[RunId, int, int], ReadRunEventsResult]
    read_attention_events: Callable[
        [RunId | None, int | None, int, tuple[tuple[RunId, int], ...]],
        ReadAttentionEventsResult,
    ]


@dataclass(frozen=True)
class RunControlUseCases:
    """Starting, forking, answering, and cancelling a run."""

    start_published_run: Callable[
        [
            RunId,
            WorkflowRevisionHash,
            tuple[AuthoredAgentBinding, ...] | None,
            tuple[AuthoredOrder, ...],
        ],
        StartPublishedRunResult,
    ]
    fork_run: Callable[[RunId, str, str], ForkRunResult]
    answer_wait: Callable[
        [RunId, WorkflowRevisionHash, str, NodeExecutionId, WaitAnswerActor, bytes],
        AnswerWaitResult,
    ]
    reconcile_run: Callable[[ReconcileRunRequest], ReconcileRunResult]
    cancel_agent_attempt: Callable[
        [CancelAgentAttemptRequest], CancelAgentAttemptResult
    ]
    cancel_run: Callable[[RunId, str, NodeExecutionId], CancelRunResult]


@dataclass(frozen=True)
class ArtifactUseCases:
    """Publishing and reading artifact blobs."""

    publish_artifact: Callable[[bytes], PublishArtifactUseCaseResult]
    read_artifact: Callable[[ArtifactHash], ReadArtifactResult]


@dataclass(frozen=True)
class DefinitionDocumentUseCases:
    """Publishing schema, budget, tool-grant, adapter-operation, and agent-definition documents, and the library intake they arrive through."""

    publish_schema_revision: Callable[[bytes], PublishSchemaRevisionResult]
    get_schema_revision: Callable[[PublishedRevisionHash], GetSchemaRevisionResult]
    publish_budget_revision: Callable[[bytes], PublishBudgetRevisionResult]
    publish_tool_grant_revision: Callable[[bytes], PublishToolGrantRevisionResult]
    publish_adapter_operation_revision: Callable[
        [bytes], PublishAdapterOperationRevisionResult
    ]
    publish_agent_definition_revision: Callable[
        [bytes], PublishAgentDefinitionRevisionResult
    ]
    classify_definition_document: Callable[
        [bytes, str | None], ClassifyDefinitionDocumentResult
    ]
    admit_library_addition: Callable[
        [bytes, CatalogIntakeKind, CatalogActor, CatalogActivatedAt],
        AdmitLibraryAdditionResult,
    ]
    read_library_addition: Callable[[CatalogIntakeId], ReadLibraryAdditionResult]


@dataclass(frozen=True)
class AgentCatalogUseCases:
    """Reading agent definitions and publishing agent configuration and auth-profile revisions."""

    list_agent_definition_revisions: Callable[
        [PublishedRevisionHash | None, int], ListAgentDefinitionRevisionsResult
    ]
    get_agent_definition_revision: Callable[
        [PublishedRevisionHash], GetAgentDefinitionRevisionResult
    ]
    publish_auth_profile_revision: Callable[
        [str, int, str, str], PublishAuthProfileRevisionResult
    ]
    publish_agent_configuration_revision: Callable[
        [str, str, str, str], PublishAgentConfigurationRevisionResult
    ]
    list_agent_configuration_revisions: Callable[
        [AgentConfigurationRevisionHash | None, int],
        ListAgentConfigurationRevisionsResult,
    ]
    list_auth_profile_revisions: Callable[
        [AuthProfileRevisionHash | None, int], ListAuthProfileRevisionsResult
    ]


@dataclass(frozen=True)
class ProjectUseCases:
    """Reading the served project or projects."""

    list_projects: Callable[[], ListProjectsResult]
    get_project: Callable[[ProjectId], GetProjectResult]


@dataclass(frozen=True)
class ModelConfigurationUseCases:
    """Reading and publishing model registries and a project's model defaults."""

    get_model_registry: Callable[[str], GetModelRegistryResult]
    publish_model_registry: Callable[
        [str, int, tuple[tuple[str, str], ...]], PublishModelRegistryUseCaseResult
    ]
    validate_model_registry_entry: Callable[
        [str, str], PublishModelRegistryUseCaseResult
    ]
    get_project_model_defaults: Callable[[str], GetProjectModelDefaultsResult]
    publish_project_model_defaults: Callable[
        [str, int, tuple[tuple[int, str, str, str, str], ...]],
        PublishProjectModelDefaultsUseCaseResult,
    ]
    get_project_model_resolution: Callable[
        [str, str, tuple[tuple[str, str], ...]], GetProjectModelResolutionResult
    ]


@dataclass(frozen=True)
class CatalogLineageUseCases:
    """Naming, founding, admitting to, and retiring a catalog lineage."""

    resolve_catalog_name: Callable[
        [RevisionKind, CatalogLineageQuery, object], CatalogNameResult
    ]
    found_catalog_lineage: Callable[
        [
            RevisionKind,
            PublishedRevisionHash,
            CatalogLineageDisplayName | None,
            CatalogActor,
            CatalogActivatedAt,
        ],
        FoundLineageResult,
    ]
    admit_catalog_member: Callable[
        [
            RevisionKind,
            CatalogLineageId,
            PublishedRevisionHash,
            CatalogActor,
            CatalogActivatedAt,
        ],
        AdmitMemberResult,
    ]
    retire_catalog_lineage: Callable[
        [CatalogLineageId, CatalogActor, CatalogActivatedAt],
        RetireCatalogLineageUseCaseResult,
    ]


@dataclass(frozen=True)
class ProjectSourceUseCases:
    """Reading, connecting, disconnecting, and rotating a project's source connections."""

    get_project_source_connection: Callable[
        [ProjectId], GetServedProjectSourceConnectionResult
    ]
    list_project_sources: Callable[[ProjectId], ListProjectSourcesResult]
    connect_project_source: Callable[
        [ProjectId, str, str], ConnectManagedProjectSourceResult
    ]
    disconnect_project_source: Callable[
        [ProjectId, ProjectSourceId], DisconnectProjectSourceResult
    ]
    rotate_project_source_token: Callable[
        [ProjectId, ProjectSourceId, str], RotateProjectSourceTokenResult
    ]


@dataclass(frozen=True)
class QueueUseCases:
    """Reading and changing the queue projection, and the tracker import and redeploy status reads beside it."""

    confirm_queue_proposal: Callable[
        [ConfirmQueueProposal], ConfirmQueueProposalOutcome
    ]
    plan_queue_item: Callable[[PlanQueueItem], PlanQueueItemOutcome]
    put_queue_project_policy: Callable[
        [QueueProjectPolicyRevision, int], PutQueueProjectPolicyOutcome
    ]
    list_queue_items: Callable[[QueueItemId | None, int], ListQueueItemsOutcome]
    import_project_source_issues: Callable[[], ImportProjectSourceIssuesOutcome]
    read_redeploy_status: Callable[[], ReadRedeployStatusResult]


@dataclass(frozen=True)
class ApiUseCases:
    """The application calls, already bound to their ports by the composition.

    Every field is a call, not a protocol: the port is spent at composition time,
    and what a route holds is the decision, whose result type belongs to
    `atelier2.application`. A field annotated with anything that resolves to
    `atelier2.ports` would hand the port straight back through this record — which
    is the evasion `scripts/check_architecture.py` reads these annotations for.

    The calls stay synchronous because admitting one to the process-wide query
    budget is the API's decision, not the application's: the route runs them
    through its own bounded runner and owns the refusal a full budget produces.

    The fields stay this flat record for the same reason `scripts/check_architecture.py`
    reads them at all: the check judges `ApiUseCases` by walking its own directly
    annotated fields, so a field of any other shape -- including one holding
    another of these calls, grouped -- would leave that walk with nothing to
    check. A route never reads a field directly, though: it reads one of the
    read-only properties below, each handing back one domain's own contract,
    declared just above this class. Each such field is typed twice, once here
    and once on the contract its property builds -- the price of a check that
    only reads a flat record, paid once per field rather than left unpaid as a
    hole in what the check can see.
    """

    get_workflow_revision: Callable[[WorkflowRevisionHash], GetWorkflowRevisionResult]
    list_workflow_revisions: Callable[
        [WorkflowRevisionHash | None, int], ListWorkflowRevisionsResult
    ]
    list_described_workflow_revisions: Callable[
        [WorkflowRevisionHash | None, int], ListDescribedWorkflowRevisionsResult
    ]
    get_run: Callable[[RunId], GetRunResult]
    get_node_detail: Callable[[RunId, str], GetNodeDetailUseCaseResult]
    list_runs: Callable[[RunId | None, int, RunState | None], ListRunsResult]
    prepare_run_events: Callable[[RunId, int], PrepareRunEventsResult]
    read_run_events: Callable[[RunId, int, int], ReadRunEventsResult]
    read_attention_events: Callable[
        [RunId | None, int | None, int, tuple[tuple[RunId, int], ...]],
        ReadAttentionEventsResult,
    ]
    publish_workflow_revision: Callable[[bytes], PublishWorkflowRevisionResult]
    publish_artifact: Callable[[bytes], PublishArtifactUseCaseResult]
    read_artifact: Callable[[ArtifactHash], ReadArtifactResult]
    publish_schema_revision: Callable[[bytes], PublishSchemaRevisionResult]
    get_schema_revision: Callable[[PublishedRevisionHash], GetSchemaRevisionResult]
    publish_budget_revision: Callable[[bytes], PublishBudgetRevisionResult]
    publish_tool_grant_revision: Callable[[bytes], PublishToolGrantRevisionResult]
    publish_adapter_operation_revision: Callable[
        [bytes], PublishAdapterOperationRevisionResult
    ]
    publish_agent_definition_revision: Callable[
        [bytes], PublishAgentDefinitionRevisionResult
    ]
    classify_definition_document: Callable[
        [bytes, str | None], ClassifyDefinitionDocumentResult
    ]
    admit_library_addition: Callable[
        [bytes, CatalogIntakeKind, CatalogActor, CatalogActivatedAt],
        AdmitLibraryAdditionResult,
    ]
    read_library_addition: Callable[[CatalogIntakeId], ReadLibraryAdditionResult]
    list_agent_definition_revisions: Callable[
        [PublishedRevisionHash | None, int], ListAgentDefinitionRevisionsResult
    ]
    get_agent_definition_revision: Callable[
        [PublishedRevisionHash], GetAgentDefinitionRevisionResult
    ]
    publish_auth_profile_revision: Callable[
        [str, int, str, str], PublishAuthProfileRevisionResult
    ]
    publish_agent_configuration_revision: Callable[
        [str, str, str, str], PublishAgentConfigurationRevisionResult
    ]
    list_agent_configuration_revisions: Callable[
        [AgentConfigurationRevisionHash | None, int],
        ListAgentConfigurationRevisionsResult,
    ]
    list_auth_profile_revisions: Callable[
        [AuthProfileRevisionHash | None, int],
        ListAuthProfileRevisionsResult,
    ]
    list_projects: Callable[[], ListProjectsResult]
    get_project: Callable[[ProjectId], GetProjectResult]
    get_model_registry: Callable[[str], GetModelRegistryResult]
    publish_model_registry: Callable[
        [str, int, tuple[tuple[str, str], ...]],
        PublishModelRegistryUseCaseResult,
    ]
    validate_model_registry_entry: Callable[
        [str, str], PublishModelRegistryUseCaseResult
    ]
    get_project_model_defaults: Callable[[str], GetProjectModelDefaultsResult]
    publish_project_model_defaults: Callable[
        [str, int, tuple[tuple[int, str, str, str, str], ...]],
        PublishProjectModelDefaultsUseCaseResult,
    ]
    get_project_model_resolution: Callable[
        [str, str, tuple[tuple[str, str], ...]], GetProjectModelResolutionResult
    ]
    start_published_run: Callable[
        [
            RunId,
            WorkflowRevisionHash,
            tuple[AuthoredAgentBinding, ...] | None,
            tuple[AuthoredOrder, ...],
        ],
        StartPublishedRunResult,
    ]
    fork_run: Callable[[RunId, str, str], ForkRunResult]
    answer_wait: Callable[
        [
            RunId,
            WorkflowRevisionHash,
            str,
            NodeExecutionId,
            WaitAnswerActor,
            bytes,
        ],
        AnswerWaitResult,
    ]
    reconcile_run: Callable[[ReconcileRunRequest], ReconcileRunResult]
    cancel_agent_attempt: Callable[
        [CancelAgentAttemptRequest], CancelAgentAttemptResult
    ]
    cancel_run: Callable[[RunId, str, NodeExecutionId], CancelRunResult]
    resolve_catalog_name: Callable[
        [RevisionKind, CatalogLineageQuery, object], CatalogNameResult
    ]
    found_catalog_lineage: Callable[
        [
            RevisionKind,
            PublishedRevisionHash,
            CatalogLineageDisplayName | None,
            CatalogActor,
            CatalogActivatedAt,
        ],
        FoundLineageResult,
    ]
    admit_catalog_member: Callable[
        [
            RevisionKind,
            CatalogLineageId,
            PublishedRevisionHash,
            CatalogActor,
            CatalogActivatedAt,
        ],
        AdmitMemberResult,
    ]
    retire_catalog_lineage: Callable[
        [CatalogLineageId, CatalogActor, CatalogActivatedAt],
        RetireCatalogLineageUseCaseResult,
    ]
    get_project_source_connection: Callable[
        [ProjectId], GetServedProjectSourceConnectionResult
    ]
    list_project_sources: Callable[[ProjectId], ListProjectSourcesResult]
    connect_project_source: Callable[
        [ProjectId, str, str], ConnectManagedProjectSourceResult
    ]
    disconnect_project_source: Callable[
        [ProjectId, ProjectSourceId], DisconnectProjectSourceResult
    ]
    rotate_project_source_token: Callable[
        [ProjectId, ProjectSourceId, str], RotateProjectSourceTokenResult
    ]
    confirm_queue_proposal: Callable[
        [ConfirmQueueProposal], ConfirmQueueProposalOutcome
    ]
    plan_queue_item: Callable[[PlanQueueItem], PlanQueueItemOutcome]
    put_queue_project_policy: Callable[
        [QueueProjectPolicyRevision, int], PutQueueProjectPolicyOutcome
    ]
    list_queue_items: Callable[[QueueItemId | None, int], ListQueueItemsOutcome]
    import_project_source_issues: Callable[[], ImportProjectSourceIssuesOutcome]
    read_redeploy_status: Callable[[], ReadRedeployStatusResult]

    @property
    def workflow_revisions(self) -> WorkflowRevisionUseCases:
        return WorkflowRevisionUseCases(
            get_workflow_revision=self.get_workflow_revision,
            list_workflow_revisions=self.list_workflow_revisions,
            list_described_workflow_revisions=self.list_described_workflow_revisions,
            publish_workflow_revision=self.publish_workflow_revision,
        )

    @property
    def runs(self) -> RunUseCases:
        return RunUseCases(
            get_run=self.get_run,
            get_node_detail=self.get_node_detail,
            list_runs=self.list_runs,
            prepare_run_events=self.prepare_run_events,
            read_run_events=self.read_run_events,
            read_attention_events=self.read_attention_events,
        )

    @property
    def run_control(self) -> RunControlUseCases:
        return RunControlUseCases(
            start_published_run=self.start_published_run,
            fork_run=self.fork_run,
            answer_wait=self.answer_wait,
            reconcile_run=self.reconcile_run,
            cancel_agent_attempt=self.cancel_agent_attempt,
            cancel_run=self.cancel_run,
        )

    @property
    def artifacts(self) -> ArtifactUseCases:
        return ArtifactUseCases(
            publish_artifact=self.publish_artifact,
            read_artifact=self.read_artifact,
        )

    @property
    def definitions(self) -> DefinitionDocumentUseCases:
        return DefinitionDocumentUseCases(
            publish_schema_revision=self.publish_schema_revision,
            get_schema_revision=self.get_schema_revision,
            publish_budget_revision=self.publish_budget_revision,
            publish_tool_grant_revision=self.publish_tool_grant_revision,
            publish_adapter_operation_revision=self.publish_adapter_operation_revision,
            publish_agent_definition_revision=self.publish_agent_definition_revision,
            classify_definition_document=self.classify_definition_document,
            admit_library_addition=self.admit_library_addition,
            read_library_addition=self.read_library_addition,
        )

    @property
    def agent_catalog(self) -> AgentCatalogUseCases:
        return AgentCatalogUseCases(
            list_agent_definition_revisions=self.list_agent_definition_revisions,
            get_agent_definition_revision=self.get_agent_definition_revision,
            publish_auth_profile_revision=self.publish_auth_profile_revision,
            publish_agent_configuration_revision=self.publish_agent_configuration_revision,
            list_agent_configuration_revisions=self.list_agent_configuration_revisions,
            list_auth_profile_revisions=self.list_auth_profile_revisions,
        )

    @property
    def projects(self) -> ProjectUseCases:
        return ProjectUseCases(
            list_projects=self.list_projects,
            get_project=self.get_project,
        )

    @property
    def model_configuration(self) -> ModelConfigurationUseCases:
        return ModelConfigurationUseCases(
            get_model_registry=self.get_model_registry,
            publish_model_registry=self.publish_model_registry,
            validate_model_registry_entry=self.validate_model_registry_entry,
            get_project_model_defaults=self.get_project_model_defaults,
            publish_project_model_defaults=self.publish_project_model_defaults,
            get_project_model_resolution=self.get_project_model_resolution,
        )

    @property
    def catalog_lineage(self) -> CatalogLineageUseCases:
        return CatalogLineageUseCases(
            resolve_catalog_name=self.resolve_catalog_name,
            found_catalog_lineage=self.found_catalog_lineage,
            admit_catalog_member=self.admit_catalog_member,
            retire_catalog_lineage=self.retire_catalog_lineage,
        )

    @property
    def project_sources(self) -> ProjectSourceUseCases:
        return ProjectSourceUseCases(
            get_project_source_connection=self.get_project_source_connection,
            list_project_sources=self.list_project_sources,
            connect_project_source=self.connect_project_source,
            disconnect_project_source=self.disconnect_project_source,
            rotate_project_source_token=self.rotate_project_source_token,
        )

    @property
    def queue(self) -> QueueUseCases:
        return QueueUseCases(
            confirm_queue_proposal=self.confirm_queue_proposal,
            plan_queue_item=self.plan_queue_item,
            put_queue_project_policy=self.put_queue_project_policy,
            list_queue_items=self.list_queue_items,
            import_project_source_issues=self.import_project_source_issues,
            read_redeploy_status=self.read_redeploy_status,
        )


@dataclass(frozen=True)
class ApiContext:
    """Everything a route needs that the composition decided once, at startup.

    A route reads it through one dependency instead of closing over the
    variables of `create_app`, which is what makes the route an importable,
    separately callable object rather than a local of a builder function.
    """

    source_commit: str
    source_tree: str
    serve_started_at: RecordedAt
    seat: SeatReader
    use_cases: ApiUseCases
    ports: ApiPorts
    limits: ApiLimits
    control_runner: BoundedQueryRunner
    event_runner: BoundedQueryRunner
    workflow_projection_limit: WorkflowPublicationLimits
    event_poll_backoff: EventPollBackoff
    request_queue_sweep: Callable[[], None] | None = None


def install_api_context(app: FastAPI, context: ApiContext) -> None:
    app.state.api_context = context


async def api_context(request: Request) -> ApiContext:
    """Hand the routes the context the composition installed.

    Declared async on purpose: FastAPI runs a non-coroutine dependency through
    `run_in_threadpool`, which would put a worker-thread hop and a slot of the
    process-wide thread limiter on the request path of every endpoint — for an
    attribute read the routes used to get for free from a closure.
    """

    context: ApiContext = request.app.state.api_context
    return context


api_context_dependency = Depends(api_context)
