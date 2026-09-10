"""Drivers that reach durable runs through the entry points production uses.

The API publishes a workflow revision and then starts a published run; the run
workflow prepares a graph action and starts the effect workflow from inside its
own workflow context. A test cannot enter that context, so the effect launch is
enqueued through the DBOS client here instead. Everything else below calls the
same use cases and adapter methods the served application calls.
"""

from __future__ import annotations

import sqlalchemy as sa
from dbos import DBOSClient, EnqueueOptions
from sqlalchemy.engine import Engine

from atelier2.adapters.dbos.advancer import prepare_graph_action as _prepare
from atelier2.adapters.dbos.agent_attempt_store import DbosAgentAttemptStore
from atelier2.adapters.dbos.agent_catalog import DbosAgentConfigurationCatalog
from atelier2.adapters.dbos.catalog_store import DbosCatalogStore
from atelier2.adapters.dbos.names import EFFECT_WORKFLOW_NAME, QUEUE_NAME
from atelier2.adapters.dbos.reconciler import DbosEffectReconcileCommander
from atelier2.adapters.dbos.run_store import DbosWaitAnswerer
from atelier2.adapters.dbos.run_transitions import run_from_record_with_bindings
from atelier2.adapters.dbos.runtime import DbosRuntime, DbosRuntimeSettings
from atelier2.adapters.dbos.schema import runs as runs_table
from atelier2.adapters.dbos.starter import (
    DbosDurableRunStarter,
    DbosWorkflowRevisionPublisher,
)
from atelier2.adapters.dbos.transactions import canonical_write_transaction
from atelier2.adapters.dbos.workflow_ids import effect_workflow_id_for
from atelier2.adapters.yaml_workflows import parse_workflow_document
from atelier2.application.answer_wait import (
    AnswerAcceptedPending,
    AnswerExistingPending,
    answer_wait_result,
)
from atelier2.application.publish_workflow_revision import (
    PublicationCreated,
    PublicationExisting,
    publish_workflow_revision,
)
from atelier2.application.reconcile_effect import (
    ReconciliationAcceptedPending,
    ReconciliationExistingApplied,
    ReconciliationExistingPending,
    ReconciliationExistingRejected,
    reconcile_effect_result,
)
from atelier2.application.start_published_run import (
    AuthoredAgentBinding,
    RunCreated,
    RunExisting,
    start_published_run,
)
from atelier2.contracts.agents import (
    AgentConfigurationRevision,
    AgentConfigurationRevisionFormatVersion,
    AgentExecutionCapability,
    AgentExecutionRequestV2,
    AgentExecutionResult,
    AgentExecutorOperationalIdentity,
    AgentExecutorRevision,
    AuthMode,
    AuthProfileRevision,
    ProviderId,
    ResolvedAgentBinding,
)
from atelier2.contracts.effects import (
    EffectAdapterBinding,
    EffectIntent,
    EffectIntentSnapshot,
    ReconcileCommand,
    ReconcileCommandSnapshot,
)
from atelier2.contracts.executions import (
    NodeExecutionId,
    SubmitWaitAnswerRequest,
    WaitAnswerSnapshot,
)
from atelier2.contracts.revisions_v3 import PublishedRevision
from atelier2.contracts.run_bindings import AnyRun, RunV3
from atelier2.contracts.runs import (
    Run,
    RunId,
    RunState,
    WorkflowRevision,
    WorkflowRevisionHash,
)
from atelier2.ports.agent_configurations import (
    AgentConfigurationRevisionCreated,
    AgentConfigurationRevisionExisting,
    AuthProfileRevisionCreated,
    AuthProfileRevisionExisting,
)
from atelier2.ports.agent_executions import AgentExecutorRegistry
from atelier2.ports.durable_runs import (
    AnyStartPublishedRunRequest,
    DurablePublishedRunResult,
    DurableRunCreated,
)
from atelier2.ports.published_revisions import (
    PublishedRevisionCreated,
    PublishedRevisionExisting,
)
from tests.scenarios.agents import (
    agent_attempt_execution,
    publish_checked_model_registry,
)
from tests.scenarios.api import permissive_projection_limit

NO_AGENT_EXECUTORS = AgentExecutorRegistry()
"""What an unbound run binds: no executor at all, said rather than defaulted into."""

V3_PROVIDER = ProviderId("exact")
"""The provider every V3 scenario binds, matching `failing_agent_executor_factory`."""

V3_EXECUTOR_REVISION = AgentExecutorRevision("exact/v1")
V3_OPERATIONAL_IDENTITY = "exact-operation"
V3_MODEL = "opus"
V3_PROFILE_ID = "max"


class AdmittingStartJudge:
    """A queue start judge that would let every start through.

    The scripted or patched starter a sweep scenario hands in then decides
    alone, exactly as it would with no judge asked first.
    """

    def start_published(
        self, request: AnyStartPublishedRunRequest
    ) -> DurablePublishedRunResult:
        return DurableRunCreated(
            Run(request.run_id, request.revision_hash, RunState.STARTED, "final", 0, 0)
        )


def publish_pinned_revisions(engine: Engine, *revisions: PublishedRevision) -> None:
    """Publish every catalog revision a workflow document pins before starting it."""
    store = DbosCatalogStore(engine)
    for revision in revisions:
        published = store.publish_revision(revision)
        assert isinstance(
            published, (PublishedRevisionCreated, PublishedRevisionExisting)
        ), published


def publish_revision(engine: Engine, revision: WorkflowRevision) -> None:
    result = publish_workflow_revision(
        revision.document,
        DbosWorkflowRevisionPublisher(engine),
        parse_workflow_document,
        permissive_projection_limit(),
        DbosCatalogStore(engine),
    )
    assert isinstance(result, (PublicationCreated, PublicationExisting)), result


def publish_v3_agent_bindings(
    engine: Engine,
    agent_executor_registry: AgentExecutorRegistry,
    roles: tuple[str, ...] = ("builder",),
) -> tuple[AuthoredAgentBinding, ...]:
    """Everything a V3 start needs before it can name a role, published once.

    A format-3 run resolves each declared role to an agent configuration, which
    resolves to an auth profile and to an executor the registry holds, and the
    model must be registered as checked before a start will bind it. Every
    scenario that drives a V3 run needs that same four-step publication, so it
    lives here rather than being reinvented per file.
    """

    catalog = DbosAgentConfigurationCatalog(engine, agent_executor_registry)
    auth = AuthProfileRevision(V3_PROFILE_ID, 1, V3_PROVIDER, AuthMode.SUBSCRIPTION)
    published_auth = catalog.publish_auth_profile_revision(auth)
    assert isinstance(
        published_auth, (AuthProfileRevisionCreated, AuthProfileRevisionExisting)
    ), published_auth
    configuration = AgentConfigurationRevision(
        V3_MODEL,
        auth.revision_hash,
        V3_EXECUTOR_REVISION,
        AgentExecutionCapability.HEADLESS,
        AgentConfigurationRevisionFormatVersion.V2,
    )
    published_configuration = catalog.publish_agent_configuration_revision(
        configuration
    )
    assert isinstance(
        published_configuration,
        (AgentConfigurationRevisionCreated, AgentConfigurationRevisionExisting),
    ), published_configuration
    publish_checked_model_registry(engine, V3_PROVIDER, (configuration,))
    return tuple(
        AuthoredAgentBinding(role, configuration.revision_hash.value) for role in roles
    )


def start_published_v3_run(
    engine: Engine,
    settings: DbosRuntimeSettings,
    run_id: RunId,
    revision: WorkflowRevision,
    agent_executor_registry: AgentExecutorRegistry,
    roles: tuple[str, ...] = ("builder",),
) -> AnyRun:
    """Publish a format-3 document with its bindings and start its run.

    It goes through the same use case the API calls. `roles` names every role the document declares; the caller says which
    they are because only the document knows. A document declaring no role at
    all publishes no binding and starts with an empty set.
    """

    bindings = (
        publish_v3_agent_bindings(engine, agent_executor_registry, roles)
        if roles
        else ()
    )
    publish_revision(engine, revision)
    result = start_published_run(
        run_id,
        revision.revision_hash,
        bindings,
        DbosDurableRunStarter(engine, settings, agent_executor_registry),
    )
    assert isinstance(result, (RunCreated, RunExisting)), result
    return result.run


def complete_v3_agent_node(
    runtime: DbosRuntime, run_id: RunId, node_id: str, job: bytes, output: bytes
) -> None:
    """Drive one V3 agent node to success through the real attempt store.

    Prepare, claim, and complete are the exact writes the node workflow
    performs, so a scenario that parks a run between two nodes without
    launching the runtime advances it here rather than hand-writing events.
    `job` is what the composition owner hands the node -- for a first node
    without inputs, its instruction bytes.
    """

    with runtime.engine.connect() as connection:
        record = (
            connection.execute(
                sa.select(runs_table).where(runs_table.c.run_id == run_id.value)
            )
            .mappings()
            .one()
        )
        run = run_from_record_with_bindings(connection, record)
    assert isinstance(run, RunV3), run
    binding = run.agent_bindings[0]
    request = AgentExecutionRequestV2(
        NodeExecutionId.for_node(run_id, run.revision_hash, node_id),
        run_id,
        run.revision_hash,
        node_id,
        ResolvedAgentBinding(binding.role, binding.configuration, binding.auth_profile),
        AgentExecutorOperationalIdentity(V3_OPERATIONAL_IDENTITY),
        job,
    )
    execution = agent_attempt_execution(request)
    store = DbosAgentAttemptStore(runtime.engine, runtime.settings.application_version)
    store.prepare(execution)
    store.claim(execution)
    store.complete_success(execution, AgentExecutionResult(output))


def prepare_graph_action(
    engine: Engine,
    run_id: RunId,
    revision_hash: WorkflowRevisionHash,
    effect_adapter_binding: EffectAdapterBinding,
) -> EffectIntentSnapshot:
    """Run the run workflow's action-preparation transaction step alone."""
    with canonical_write_transaction(engine) as connection:
        return _prepare(connection, run_id, revision_hash, effect_adapter_binding)


def launch_effect_workflow(
    engine: Engine, settings: DbosRuntimeSettings, intent: EffectIntent
) -> None:
    """Start the effect workflow the run workflow starts after preparing."""
    client = DBOSClient(system_database_engine=engine, use_listen_notify=False)
    try:
        options: EnqueueOptions = {
            "workflow_name": EFFECT_WORKFLOW_NAME,
            "queue_name": QUEUE_NAME,
            "workflow_id": effect_workflow_id_for(intent.binding.logical_key),
            "app_version": settings.application_version,
        }
        client.enqueue(
            options,
            intent.binding.logical_key.value,
            intent.binding.workflow_revision_hash.value,
        )
    finally:
        client.destroy()


def prepare_and_launch_graph_action(
    engine: Engine,
    settings: DbosRuntimeSettings,
    run_id: RunId,
    revision_hash: WorkflowRevisionHash,
    effect_adapter_binding: EffectAdapterBinding,
) -> EffectIntent:
    snapshot = prepare_graph_action(
        engine, run_id, revision_hash, effect_adapter_binding
    )
    launch_effect_workflow(engine, settings, snapshot.intent)
    return snapshot.intent


def submit_wait_answer(
    engine: Engine, application_version: str, request: SubmitWaitAnswerRequest
) -> WaitAnswerSnapshot:
    result = answer_wait_result(
        request.run_id,
        request.revision_hash,
        request.node_id,
        request.expected_node_execution_id,
        request.actor,
        request.answer_bytes,
        DbosWaitAnswerer(engine, application_version),
    )
    assert isinstance(result, (AnswerAcceptedPending, AnswerExistingPending)), result
    return result.snapshot


def submit_reconcile_command(
    engine: Engine, settings: DbosRuntimeSettings, command: ReconcileCommand
) -> ReconcileCommandSnapshot:
    result = reconcile_effect_result(
        command, DbosEffectReconcileCommander(engine, settings)
    )
    assert isinstance(
        result,
        (
            ReconciliationAcceptedPending,
            ReconciliationExistingPending,
            ReconciliationExistingApplied,
            ReconciliationExistingRejected,
        ),
    ), result
    return result.snapshot
