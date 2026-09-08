from __future__ import annotations

import logging
import sqlite3
import threading
import time
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any, assert_never

import sqlalchemy as sa
from dbos import DBOS, DBOSConfig, SQLAlchemyDatasource, WorkflowStatusString
from sqlalchemy import event
from sqlalchemy.engine import Connection, Engine

from atelier2.adapters.agent_claim_cli import (
    AGENT_CLAIM_ADAPTER_REVISION,
    AgentClaimCli,
)
from atelier2.adapters.agent_processes import (
    AgentProcessSupervisor,
    delegated_cgroup_root,
)
from atelier2.adapters.agent_workspaces import (
    LocalAgentAttemptWorkspaceOwner,
    attested_directory,
)
from atelier2.adapters.claim_checkouts import LocalClaimCheckouts
from atelier2.adapters.dbos.agent_attempt_store import DbosAgentAttemptStore
from atelier2.adapters.dbos.artifact_store import DbosArtifactStore
from atelier2.adapters.dbos.catalog_store import DbosCatalogStore
from atelier2.adapters.dbos.effect_store import converge_driverless_effect_intents
from atelier2.adapters.dbos.host_configuration import (
    append_project_root,
    project_root_for,
)
from atelier2.adapters.dbos.names import QUEUE_NAME
from atelier2.adapters.dbos.queue_projection_store import DbosQueueProjectionStore
from atelier2.adapters.dbos.queue_sweep import QueueSweepTicker
from atelier2.adapters.dbos.schema import (
    agent_attempts,
    agent_configuration_revisions,
    auth_profile_revisions,
    effect_intents,
    initialize_schema,
    run_agent_bindings,
    run_events,
    runs,
)
from atelier2.adapters.dbos.uncontinuable_runs import (
    DbosUncontinuableRunStore,
    retag_stranded_continuations,
)
from atelier2.adapters.dbos.work_item_claims import (
    WorkItemClaimLedger,
    close_checkouts_of_terminal_runs,
)
from atelier2.adapters.dbos.workflow import (
    AgentExecutorMap,
    register_durable_run_workflow,
)
from atelier2.adapters.dbos.workflow_ids import (
    effect_workflow_id_for,
    node_workflow_id_for,
    reconcile_workflow_id_for,
)
from atelier2.adapters.project_verification import declared_project
from atelier2.adapters.yaml_workflows import parse_workflow_document
from atelier2.application.advance_queue import (
    QueueAutomationLabelUnset,
    QueueAutomationSourceUnreadable,
    QueueLabelAdmissionOutcome,
    QueueLabelAdmissionsDecided,
    _queue_label_admission_declined_reason,
    admit_queue_items_by_label,
    advance_queue,
)
from atelier2.application.converge_driverless_attempts import (
    converge_driverless_attempts,
)
from atelier2.application.converge_uncontinuable_runs import (
    converge_uncontinuable_runs,
)
from atelier2.contracts.adapter_operations_v3 import AdapterOperationName
from atelier2.contracts.agent_permissions import GRANTS_NOTHING
from atelier2.contracts.agents import (
    AgentConfigurationRevisionHash,
    AgentExecutionCapability,
    AgentExecutorRevision,
    ProviderId,
)
from atelier2.contracts.catalog_v3 import CatalogLineageDisplayName
from atelier2.contracts.effects import (
    AdapterOperationalIdentity,
    AdapterRevision,
    EffectAdapterBinding,
    EffectDestination,
    EffectIntentState,
    LogicalEffectKey,
    ReconcileCommandId,
)
from atelier2.contracts.executions import (
    NodeExecutionId,
    logical_effect_key_for,
    work_item_claim_effect_key,
)
from atelier2.contracts.hashing import Sha256Hash
from atelier2.contracts.host_configuration import (
    PROJECT_UNKNOWN,
    ProjectId,
    ProjectRootMissing,
    ProjectUnknown,
)
from atelier2.contracts.provider_probe_receipts import (
    PROVIDER_CANARY_WORKFLOW_NAMES,
    ProviderProbeReceipt,
    ProviderProbeReceiptRefused,
    read_provider_probe_receipt,
)
from atelier2.contracts.revisions_v3 import RevisionKind
from atelier2.contracts.runs import WorkflowRevisionHash
from atelier2.contracts.when import recorded_instant
from atelier2.contracts.workflow_formats import WorkflowFormatVersion
from atelier2.ports.agent_executions import (
    AgentExecutorCarrier,
    AgentExecutorFactoryV2,
    AgentExecutorKey,
    AgentExecutorManifestEntry,
    AgentExecutorRegistration,
    AgentExecutorRegistry,
    AgentExecutorV2,
    ProviderProbeReceiptGate,
)
from atelier2.ports.effects import (
    EffectAdapter,
    EffectAdapterFactory,
    EffectAdapterRegistration,
    EffectAdapterRegistry,
    OpenEffectAdapterRegistry,
)
from atelier2.ports.issue_observation import TrackerItemSource
from atelier2.ports.project_verification import DeclaredProject
from atelier2.ports.published_revisions import CatalogNameFound

_LOG = logging.getLogger("atelier2")

EXECUTOR_ID = "atelier2-local"
SQLITE_LOCK_TIMEOUT_SECONDS = 30.0
_SQLITE_WAL_RETRY_SECONDS = 0.01
_SQLITE_RETRYABLE_ERRORS = frozenset((sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED))
_SHUTDOWN_WORKFLOW_COMPLETION_SECONDS = 1
AGENT_TERMINATION_GRACE_SECONDS = 2.0
LOCAL_EXECUTION_PERMISSION_POLICY = GRANTS_NOTHING
"""What a locally supervised provider process may ask this deployment for.

The closed policy today, and therefore a refusal of everything: no provider
channel this runtime opens can ask yet (ADR 0020 step 2 brings the first). It is
bound here, at the composition root, and handed down as a dispatch parameter,
because what a deployment permits is the deployment's own fact -- never a
workflow's to look up.
"""


CLAIM_CHECKOUT_ROOT_NAME = "claim-checkouts"
"""The claim checkouts' root, a sibling of the agent scratch root."""


class DbosRuntimeBindingConflict(RuntimeError):
    """A second, incompatible DBOS binding was requested inside one process."""


class DbosRuntimeLeaseClosed(RuntimeError):
    """A released lease on the process DBOS runtime was used again."""


class AgentProcessSupervisorUnavailable(RuntimeError):
    """The runtime has no local process authority for V2/V3 execution."""


@dataclass(frozen=True)
class DbosRuntimeBinding:
    """What a process globally binds while it owns the DBOS runtime.

    The resource-free adapter binding participates in compatibility so a
    refused lease cannot open or mutate an unrelated external destination.
    """

    canonical_database_path: Path
    application_version: str
    agent_executors_v2: tuple[AgentExecutorManifestEntry, ...]
    effect_adapters: tuple[EffectAdapterBinding, ...]
    agent_process_control_root: Path | None
    agent_process_cgroup_root: Path | None
    agent_scratch_root: Path | None
    project_id: ProjectId | None
    agent_termination_grace_seconds: float | None


@dataclass(frozen=True)
class DbosRuntimeSettings:
    database_path: Path
    application_version: str
    agent_process_control_root: Path | None = None
    agent_process_cgroup_root: Path | None = None
    agent_scratch_root: Path | None = None
    project_id: ProjectId | None = None
    bootstrap_project_root: Path | None = None
    aco_executable: Path | None = None
    agent_termination_grace_seconds: float = AGENT_TERMINATION_GRACE_SECONDS
    sqlite_lock_timeout_seconds: float = SQLITE_LOCK_TIMEOUT_SECONDS
    # The receipt gate (`#1013`): declared together or not at all -- a
    # directory with no deployment identity to judge foreign evidence against
    # is a half-armed gate, not a safer one. `None` for both is what every
    # `DbosRuntimeSettings` outside `HostSettings.runtime_settings()` already
    # passes; `is_startable` then keeps its unarmed, factory-and-capability
    # answer exactly, since none of those registries model live provider
    # evidence at all. `provider_probe_receipt_provider_layer_digest` is this
    # deployment's own `host.provider_canary.provider_layer_digest()` (#1124):
    # what the gate actually compares a receipt against, not the whole
    # `source_commit` -- a redeploy that leaves the provider layer's own bytes
    # unchanged leaves every receipt proven.
    provider_probe_receipt_directory: Path | None = None
    provider_probe_receipt_provider_layer_digest: Sha256Hash | None = None

    def __post_init__(self) -> None:
        if not self.application_version.strip():
            raise ValueError("application_version must be nonempty")
        if self.agent_termination_grace_seconds <= 0:
            raise ValueError("agent termination grace must be positive")
        if self.sqlite_lock_timeout_seconds <= 0:
            raise ValueError("the SQLite lock timeout must be positive")
        if self.bootstrap_project_root is not None and self.project_id is None:
            raise ValueError(
                "a bootstrap project root writes the host configuration "
                "channel, so it needs a project id"
            )
        if self.project_id is not None and not isinstance(self.project_id, ProjectId):
            raise TypeError("project id must use its typed contract")
        receipt_gate_fields = (
            self.provider_probe_receipt_directory,
            self.provider_probe_receipt_provider_layer_digest,
        )
        declared_receipt_gate = tuple(
            field for field in receipt_gate_fields if field is not None
        )
        if declared_receipt_gate and len(declared_receipt_gate) != len(
            receipt_gate_fields
        ):
            raise ValueError(
                "a receipt gate needs its receipt directory and this "
                "deployment's provider layer digest declared together, not in part"
            )
        if self.provider_probe_receipt_provider_layer_digest is not None and not (
            isinstance(self.provider_probe_receipt_provider_layer_digest, Sha256Hash)
        ):
            raise TypeError(
                "provider_probe_receipt_provider_layer_digest must use its "
                "typed contract"
            )

    def process_control_root(self) -> Path:
        root = self.agent_process_control_root
        return (
            (self.database_path.parent / ".atelier2-agent-control").resolve()
            if root is None
            else root.resolve()
        )

    def process_cgroup_root(self) -> Path:
        root = self.agent_process_cgroup_root
        return delegated_cgroup_root() if root is None else root.resolve()

    def binding(
        self,
        agent_executors_v2: tuple[AgentExecutorManifestEntry, ...],
        effect_adapters: tuple[EffectAdapterBinding, ...],
    ) -> DbosRuntimeBinding:
        # Only a `LOCAL_PROCESS`-carried key needs Serve's own supervisor and
        # scratch root (`#540` C-3.6).
        process_runner_required = any(
            entry.carrier is AgentExecutorCarrier.LOCAL_PROCESS
            for entry in agent_executors_v2
        )
        return DbosRuntimeBinding(
            self.database_path.resolve(),
            self.application_version,
            agent_executors_v2,
            effect_adapters,
            self.process_control_root() if process_runner_required else None,
            self.process_cgroup_root() if process_runner_required else None,
            (
                self.agent_scratch_root.resolve()
                if process_runner_required and self.agent_scratch_root is not None
                else None
            ),
            self.project_id,
            self.agent_termination_grace_seconds if process_runner_required else None,
        )


def sqlite_url(database_path: Path) -> str:
    return f"sqlite:///{database_path.resolve()}"


def create_canonical_engine(
    database_path: Path,
    lock_timeout_seconds: float = SQLITE_LOCK_TIMEOUT_SECONDS,
) -> Engine:
    """The one engine every durable path shares, waiting as long as it was told.

    How long to wait for a busy store is the instance's answer, not the code's:
    a laptop and a loaded host disagree honestly, and the serving host passes what
    it was configured with.

    The default is the named owner itself rather than a second number, so a caller
    that says nothing still waits the one documented wait. It stays a default
    because making it required buys nothing here and costs every test that opens a
    store a line of noise: the value has one home either way.
    """

    database_path.parent.mkdir(parents=True, exist_ok=True)
    engine = sa.create_engine(
        sqlite_url(database_path),
        connect_args={
            "check_same_thread": False,
            "timeout": lock_timeout_seconds,
        },
    )

    @event.listens_for(engine, "connect")
    def configure(connection: Any, _record: Any) -> None:
        connection.isolation_level = "IMMEDIATE"
        connection.execute(f"PRAGMA busy_timeout={int(lock_timeout_seconds * 1000)}")
        connection.execute("PRAGMA foreign_keys=ON")
        _establish_wal_journal_mode(connection, lock_timeout_seconds)

    return engine


def _establish_wal_journal_mode(
    connection: Any, lock_timeout_seconds: float = SQLITE_LOCK_TIMEOUT_SECONDS
) -> None:
    deadline = time.monotonic() + lock_timeout_seconds
    while True:
        try:
            journal_mode = connection.execute("PRAGMA journal_mode=WAL").fetchone()[0]
        except sqlite3.OperationalError as error:
            if (
                error.sqlite_errorcode not in _SQLITE_RETRYABLE_ERRORS
                or time.monotonic() >= deadline
            ):
                raise
            time.sleep(_SQLITE_WAL_RETRY_SECONDS)
            continue
        if journal_mode != "wal":
            raise RuntimeError("canonical SQLite database requires WAL journal mode")
        return


@dataclass
class _BoundRuntime:
    settings: DbosRuntimeSettings
    engine: Engine
    datasource: SQLAlchemyDatasource
    agent_executor_registry: AgentExecutorRegistry
    agent_executors_v2: tuple[tuple[AgentExecutorManifestEntry, AgentExecutorV2], ...]
    effect_adapter_bindings: tuple[EffectAdapterBinding, ...]
    effect_adapters: OpenEffectAdapterRegistry
    agent_process_supervisor: AgentProcessSupervisor | None
    agent_workspace_owner: LocalAgentAttemptWorkspaceOwner | None
    declared_project: DeclaredProject | None
    tracker_item_source: TrackerItemSource | None
    work_item_claims: WorkItemClaimLedger | None = None
    leases: int = 0
    launched: bool = False
    storage_ready: bool = False
    queue_sweep: QueueSweepTicker | None = None


def _work_item_claim_ledger(
    executable: Path | None,
    project_checkout: Path | None,
    scratch_root: Path | None,
    engine: Engine,
) -> WorkItemClaimLedger | None:
    """Compose the claim boundary only for a configured, served project."""

    if executable is None or project_checkout is None:
        return None
    if scratch_root is None:
        raise DbosRuntimeBindingConflict(
            "a claim command requires an agent scratch root, because every "
            "claim is held from a checkout beside it"
        )
    return WorkItemClaimLedger(
        AgentClaimCli(executable, project_checkout),
        EffectAdapterBinding(
            AdapterRevision(AGENT_CLAIM_ADAPTER_REVISION),
            EffectDestination(str(project_checkout)),
            AdapterOperationalIdentity(str(executable)),
            AdapterOperationName.CLAIM_WORK_ITEM,
        ),
        DbosQueueProjectionStore(engine),
        LocalClaimCheckouts(
            project_checkout,
            attested_directory(scratch_root.with_name(CLAIM_CHECKOUT_ROOT_NAME)),
        ),
    )


def _project_checkout_for(engine: Engine, project_id: ProjectId | None) -> Path | None:
    """Where the project this process serves is checked out, or nothing.

    A missing mapping is `project-unknown`: naming a project with no configured
    root is the ADR 0011 service refusal, not the channel's own row miss.
    """

    if project_id is None:
        return None
    try:
        return project_root_for(engine, project_id)
    except ProjectRootMissing as missing:
        raise ProjectUnknown(
            f"{PROJECT_UNKNOWN}: project {project_id.value!r} has no configured root"
        ) from missing


def _declared_project_at(
    project_checkout: Path | None, database_path: Path
) -> DeclaredProject | None:
    """Compose the served project and place its candidates beside the database."""

    if project_checkout is None:
        return None
    return declared_project(project_checkout, database_path)


# DBOS owns this table and these tokens; read only to decide whether an open
# effect intent's own driving workflow will ever touch its adapter again.
_dbos_workflow_status = sa.table(
    "workflow_status",
    sa.column("workflow_uuid"),
    sa.column("status"),
)
_TERMINAL_WORKFLOW_STATUSES = frozenset(
    {
        WorkflowStatusString.SUCCESS.value,
        WorkflowStatusString.ERROR.value,
        WorkflowStatusString.CANCELLED.value,
    }
)
"""Statuses under which a DBOS workflow has ended for good and is never
replayed again. `MAX_RECOVERY_ATTEMPTS_EXCEEDED` is not named: every workflow
this check inspects (`durable_effect`, `durable_reconciliation`) is registered
with `max_recovery_attempts=None`, so DBOS never assigns it one."""


_EFFECT_WORKFLOW_DRIVEN_INTENT_STATES = frozenset(
    {EffectIntentState.PREPARED, EffectIntentState.CONFIRMED}
)
"""The states `durable_effect` drives -- and the only two an Agent node's own
grant redemption (`workflow.py::redeem_agent_node_effect`) ever leaves behind,
which is why the node-workflow fallback is offered exactly here."""


def _open_binding_owning_workflow_id(record: sa.Row[Any]) -> str | None:
    """Return the workflow that can still move this intent, if one exists.

    A waiting-reconciliation intent has no driver and must retain its binding.
    Agent-redeemed intents whose effect workflow was never minted are resolved
    separately through their node workflow.
    """

    state = EffectIntentState(str(record.state))
    if state in _EFFECT_WORKFLOW_DRIVEN_INTENT_STATES:
        return effect_workflow_id_for(LogicalEffectKey(str(record.logical_key)))
    if state is EffectIntentState.RECONCILING:
        return reconcile_workflow_id_for(
            ReconcileCommandId(str(record.reconciliation_owner_command_id))
        )
    return None


def _recorded_workflow_statuses(
    connection: Connection, workflow_ids: set[str]
) -> dict[str, str]:
    """What DBOS recorded for each named workflow; an unminted id is absent."""

    if not workflow_ids:
        return {}
    return {
        str(record.workflow_uuid): str(record.status)
        for record in connection.execute(
            sa.select(
                _dbos_workflow_status.c.workflow_uuid,
                _dbos_workflow_status.c.status,
            ).where(_dbos_workflow_status.c.workflow_uuid.in_(workflow_ids))
        )
    }


def _agent_redeemed_owning_workflow_ids(
    connection: Connection, records: list[sa.Row[Any]]
) -> dict[str, str]:
    """Map agent effect and claim keys to the node workflow that drove them.

    A missing execution remains unowned so binding validation fails closed.
    """

    run_ids = {str(record.run_id) for record in records}
    if not run_ids:
        return {}
    node_workflow_ids_by_key: dict[str, str] = {}
    for execution_id in _node_executions_of(connection, run_ids):
        workflow_id = node_workflow_id_for(execution_id)
        for logical_key in (
            logical_effect_key_for(execution_id),
            work_item_claim_effect_key(execution_id),
        ):
            node_workflow_ids_by_key[logical_key.value] = workflow_id
    return {
        logical_key: node_workflow_ids_by_key[logical_key]
        for record in records
        if (logical_key := str(record.logical_key)) in node_workflow_ids_by_key
    }


def _node_executions_of(
    connection: Connection, run_ids: set[str]
) -> tuple[NodeExecutionId, ...]:
    """Read reached node executions from attempts and their earlier events."""

    executions = {
        str(record.node_execution_id)
        for table in (agent_attempts, run_events)
        for record in connection.execute(
            sa.select(table.c.node_execution_id)
            .where(table.c.run_id.in_(run_ids))
            .distinct()
        )
    }
    return tuple(NodeExecutionId(execution) for execution in sorted(executions))


def _still_open_effect_intents(connection: Connection) -> list[sa.Row[Any]]:
    """Return nonterminal intents in stable refusal order.

    Abandoned intents are terminal. Every other intent stays open unless its
    effect, reconciliation, or fallback node workflow is terminal; a missing
    status therefore fails closed.
    """

    candidates = [
        record
        for record in connection.execute(
            sa.select(
                effect_intents.c.logical_key,
                effect_intents.c.run_id,
                effect_intents.c.state,
                effect_intents.c.reconciliation_owner_command_id,
                effect_intents.c.operation_name,
                effect_intents.c.adapter_revision,
                effect_intents.c.destination_identity,
                effect_intents.c.adapter_operational_identity,
            ).order_by(effect_intents.c.operation_name, effect_intents.c.logical_key)
        )
        if EffectIntentState(str(record.state)) is not EffectIntentState.ABANDONED
    ]
    owning_workflow_ids = {
        str(record.logical_key): workflow_id
        for record in candidates
        if (workflow_id := _open_binding_owning_workflow_id(record)) is not None
    }
    workflow_statuses = _recorded_workflow_statuses(
        connection, set(owning_workflow_ids.values())
    )
    unminted_effect_workflows = [
        record
        for record in candidates
        if EffectIntentState(str(record.state)) in _EFFECT_WORKFLOW_DRIVEN_INTENT_STATES
        and owning_workflow_ids[str(record.logical_key)] not in workflow_statuses
    ]
    node_workflow_ids = _agent_redeemed_owning_workflow_ids(
        connection, unminted_effect_workflows
    )
    owning_workflow_ids.update(node_workflow_ids)
    workflow_statuses.update(
        _recorded_workflow_statuses(connection, set(node_workflow_ids.values()))
    )
    terminal_workflow_ids = {
        workflow_id
        for workflow_id, status in workflow_statuses.items()
        if status in _TERMINAL_WORKFLOW_STATUSES
    }
    return [
        record
        for record in candidates
        if owning_workflow_ids.get(str(record.logical_key)) not in terminal_workflow_ids
    ]


_BINDING_CONFLICT_LOGICAL_KEY_PREVIEW_LENGTH = 32
_BINDING_CONFLICT_INTENT_PREVIEW_LIMIT = 5
"""How many offending intents the refusal message names outright.

An operator reconciling a moved identity needs enough examples to start, not
every row a large backlog produced; the omitted count still says how much is
left to look at.
"""


def _open_binding_conflict_message(
    open_effect_intents: list[sa.Row[Any]],
    effect_bindings: set[EffectAdapterBinding],
) -> str:
    offending = [
        record
        for record in open_effect_intents
        if EffectAdapterBinding(
            AdapterRevision(str(record.adapter_revision)),
            EffectDestination(str(record.destination_identity)),
            AdapterOperationalIdentity(str(record.adapter_operational_identity)),
            AdapterOperationName(str(record.operation_name)),
        )
        not in effect_bindings
    ]
    preview = offending[:_BINDING_CONFLICT_INTENT_PREVIEW_LIMIT]
    omitted_count = len(offending) - len(preview)
    named = ", ".join(
        f"{record.operation_name} intent "
        f"{str(record.logical_key)[:_BINDING_CONFLICT_LOGICAL_KEY_PREVIEW_LENGTH]}… "
        f"is still {record.state}"
        for record in preview
    )
    if omitted_count > 0:
        named = f"{named}, and {omitted_count} more"
    return (
        "runtime adapter binding differs from durable effect intents still open "
        f"for reconciliation under another identity: {named}"
    )


@dataclass(frozen=True)
class _ProjectBinding:
    declared_project: DeclaredProject | None
    work_item_claims: WorkItemClaimLedger | None
    effect_bindings: tuple[EffectAdapterBinding, ...]


@dataclass(frozen=True)
class _DurableBindingRequirements:
    open_effect_intents: list[sa.Row[Any]]
    effect_bindings: set[EffectAdapterBinding]
    agent_capabilities: set[tuple[AgentExecutorKey, AgentExecutionCapability]]


def _require_distinct_effect_stores(
    canonical_database: Path, effect_bindings: tuple[EffectAdapterBinding, ...]
) -> None:
    for effect_binding in effect_bindings:
        external_database = Path(effect_binding.operational_identity.value)
        same_existing_file = False
        if (
            external_database.is_absolute()
            and canonical_database.exists()
            and external_database.exists()
        ):
            try:
                same_existing_file = canonical_database.samefile(external_database)
            except OSError:
                same_existing_file = True
        if str(canonical_database) == str(external_database) or same_existing_file:
            raise DbosRuntimeBindingConflict(
                "canonical and external effect stores must be distinct"
            )


def _local_process_binding(
    settings: DbosRuntimeSettings, agent_registry: AgentExecutorRegistry
) -> bool:
    local_process = any(
        entry.manifest_entry.carrier is AgentExecutorCarrier.LOCAL_PROCESS
        for entry in agent_registry.entries
    )
    if local_process and settings.agent_scratch_root is None:
        raise DbosRuntimeBindingConflict(
            "serving a provider executor requires an agent scratch root, because "
            "every attempt is started in a workspace of its own"
        )
    return local_process


def _bind_project(
    engine: Engine,
    settings: DbosRuntimeSettings,
    effect_bindings: tuple[EffectAdapterBinding, ...],
) -> _ProjectBinding:
    if settings.bootstrap_project_root is not None:
        if settings.project_id is None:
            raise ValueError(
                "a bootstrap project root writes the host configuration "
                "channel, so it needs a project id"
            )
        append_project_root(
            engine, settings.project_id, settings.bootstrap_project_root
        )
    checkout = _project_checkout_for(engine, settings.project_id)
    declared_project = _declared_project_at(checkout, settings.database_path)
    work_item_claims = _work_item_claim_ledger(
        settings.aco_executable,
        checkout,
        settings.agent_scratch_root,
        engine,
    )
    if work_item_claims is not None:
        effect_bindings = (*effect_bindings, work_item_claims.binding)
    return _ProjectBinding(declared_project, work_item_claims, effect_bindings)


def _durable_binding_requirements(engine: Engine) -> _DurableBindingRequirements:
    with engine.connect() as connection:
        open_effect_intents = _still_open_effect_intents(connection)
        effect_bindings = {
            EffectAdapterBinding(
                AdapterRevision(str(record.adapter_revision)),
                EffectDestination(str(record.destination_identity)),
                AdapterOperationalIdentity(str(record.adapter_operational_identity)),
                AdapterOperationName(str(record.operation_name)),
            )
            for record in open_effect_intents
        }
        agent_capabilities = {
            (
                AgentExecutorKey(
                    ProviderId(str(record.provider_id)),
                    AgentExecutorRevision(str(record.executor_revision)),
                ),
                AgentExecutionCapability(str(record.requested_capability)),
            )
            for record in connection.execute(
                sa.select(
                    auth_profile_revisions.c.provider_id,
                    agent_configuration_revisions.c.executor_revision,
                    agent_configuration_revisions.c.requested_capability,
                )
                .select_from(runs)
                .join(
                    run_agent_bindings,
                    run_agent_bindings.c.run_id == runs.c.run_id,
                )
                .join(
                    agent_configuration_revisions,
                    agent_configuration_revisions.c.revision_hash
                    == run_agent_bindings.c.agent_configuration_revision_hash,
                )
                .join(
                    auth_profile_revisions,
                    auth_profile_revisions.c.revision_hash
                    == agent_configuration_revisions.c.auth_profile_revision_hash,
                )
                .where(
                    runs.c.workflow_format_version.in_(
                        (WorkflowFormatVersion.V2, WorkflowFormatVersion.V3)
                    ),
                    runs.c.state != "COMPLETED",
                )
                .distinct()
            )
        }
    return _DurableBindingRequirements(
        open_effect_intents, effect_bindings, agent_capabilities
    )


def _require_effect_bindings(
    requirements: _DurableBindingRequirements,
    effect_bindings: tuple[EffectAdapterBinding, ...],
) -> None:
    if not requirements.effect_bindings.issubset(set(effect_bindings)):
        raise DbosRuntimeBindingConflict(
            _open_binding_conflict_message(
                requirements.open_effect_intents, set(effect_bindings)
            )
        )


def _require_executor_bindings(
    requirements: _DurableBindingRequirements,
    agent_registry: AgentExecutorRegistry,
) -> None:
    required_keys = {key for key, _capability in requirements.agent_capabilities}
    if not required_keys.issubset(agent_registry.keys):
        raise DbosRuntimeBindingConflict(
            "runtime registry is missing a nonterminal durable executor binding"
        )


def _require_executor_capabilities(
    requirements: _DurableBindingRequirements,
    agent_registry: AgentExecutorRegistry,
) -> None:
    if any(
        capability not in agent_registry.declared_capabilities(key)
        for key, capability in requirements.agent_capabilities
    ):
        raise DbosRuntimeBindingConflict(
            "runtime registry lacks a nonterminal durable capability"
        )


def _open_agent_executors(
    agent_registry: AgentExecutorRegistry,
    opened: list[tuple[AgentExecutorManifestEntry, AgentExecutorV2]],
) -> None:
    for entry in agent_registry.entries:
        if entry.factory is not None:
            opened.append((entry.manifest_entry, entry.factory.open()))


def _cleanup_binding_resources(
    engine: Engine,
    agent_executors: list[tuple[AgentExecutorManifestEntry, AgentExecutorV2]],
    adapters: OpenEffectAdapterRegistry | None,
    supervisor: AgentProcessSupervisor | None,
    workspace_owner: LocalAgentAttemptWorkspaceOwner | None,
) -> list[BaseException]:
    cleanup_errors: list[BaseException] = []
    cleanups = []
    if supervisor is not None:
        cleanups.append(supervisor.close)
    if workspace_owner is not None:
        cleanups.append(workspace_owner.close)
    if adapters is not None:
        cleanups.append(adapters.close)
    cleanups.extend(executor.close for _entry, executor in reversed(agent_executors))
    cleanups.append(engine.dispose)
    for cleanup in cleanups:
        try:
            cleanup()
        except BaseException as cleanup_error:
            cleanup_errors.append(cleanup_error)
    return cleanup_errors


def _open_binding(
    settings: DbosRuntimeSettings,
    agent_registry: AgentExecutorRegistry,
    effect_registry: EffectAdapterRegistry,
    effect_bindings: tuple[EffectAdapterBinding, ...],
    *,
    tracker_item_source: TrackerItemSource | None,
) -> _BoundRuntime:
    canonical_database = settings.database_path.resolve()
    _require_distinct_effect_stores(canonical_database, effect_bindings)
    local_process_keys = _local_process_binding(settings, agent_registry)
    engine = create_canonical_engine(
        settings.database_path, settings.sqlite_lock_timeout_seconds
    )
    agent_executors_v2: list[tuple[AgentExecutorManifestEntry, AgentExecutorV2]] = []
    adapters: OpenEffectAdapterRegistry | None = None
    agent_process_supervisor: AgentProcessSupervisor | None = None
    agent_workspace_owner: LocalAgentAttemptWorkspaceOwner | None = None
    try:
        initialize_schema(engine)
        project_binding = _bind_project(engine, settings, effect_bindings)
        effect_bindings = project_binding.effect_bindings
        requirements = _durable_binding_requirements(engine)
        _require_effect_bindings(requirements, effect_bindings)
        _require_executor_bindings(requirements, agent_registry)
        _require_executor_capabilities(requirements, agent_registry)
        _open_agent_executors(agent_registry, agent_executors_v2)
        adapters = effect_registry.open()
        datasource = SQLAlchemyDatasource.create(
            sqlite_url(settings.database_path), engine=engine
        )
        attempt_store = DbosAgentAttemptStore(engine, settings.application_version)
        artifact_store = DbosArtifactStore(engine)
        if local_process_keys:
            agent_process_supervisor = AgentProcessSupervisor(
                attempt_store,
                settings.process_control_root(),
                settings.process_cgroup_root(),
                grace_seconds=settings.agent_termination_grace_seconds,
            )
        if local_process_keys and settings.agent_scratch_root is not None:
            agent_workspace_owner = LocalAgentAttemptWorkspaceOwner(
                settings.agent_scratch_root
            )
            # Binding the durable database is the moment a restart can tell an
            # abandoned workspace from a live one, so it is where the workspaces
            # of attempts that ended before the restart are removed.
            agent_workspace_owner.reconcile(attempt_store)
        register_durable_run_workflow(
            datasource,
            engine,
            _agent_executor_map(agent_registry, tuple(agent_executors_v2)),
            attempt_store,
            agent_process_supervisor,
            LOCAL_EXECUTION_PERMISSION_POLICY,
            agent_workspace_owner,
            project_binding.declared_project,
            artifact_store,
            adapters,
            effect_bindings,
            settings.project_id,
            project_binding.work_item_claims,
        )
    except BaseException as original:
        cleanup_errors = _cleanup_binding_resources(
            engine,
            agent_executors_v2,
            adapters,
            agent_process_supervisor,
            agent_workspace_owner,
        )
        if cleanup_errors:
            raise BaseExceptionGroup(
                "runtime open and cleanup both failed", [original, *cleanup_errors]
            ) from None
        raise
    return _BoundRuntime(
        settings,
        engine,
        datasource,
        agent_registry,
        tuple(agent_executors_v2),
        effect_bindings,
        adapters,
        agent_process_supervisor,
        agent_workspace_owner,
        project_binding.declared_project,
        tracker_item_source,
        work_item_claims=project_binding.work_item_claims,
    )


def _agent_executor_map(
    registry: AgentExecutorRegistry,
    executors: tuple[tuple[AgentExecutorManifestEntry, AgentExecutorV2], ...],
) -> AgentExecutorMap:
    """Every registered executor key mapped to what a driver needs.

    One owner for the map the durable workflow binding is composed with -- the
    opened executor where there is one, and the manifest facts either way.
    """
    opened = {manifest_entry.key: executor for manifest_entry, executor in executors}
    return {
        entry.key: (
            opened.get(entry.key),
            entry.manifest_entry.operational_identity,
            entry.manifest_entry.declared_capabilities,
            entry.manifest_entry.carrier,
        )
        for entry in registry.entries
    }


def _log_queue_label_admission(outcome: QueueLabelAdmissionOutcome) -> None:
    """Say what the automation label admitted this sweep, and what it did not.

    Both halves of this rule are soft by contract: an unreadable tracker admits
    nothing and a declined item stays exactly as durable as it was, so neither
    leaves a trace anywhere else. The sweep is therefore the only place that
    can make them visible to the operator. A project whose policy names no
    label says nothing here -- that is a steady state, not an event.
    """

    match outcome:
        case QueueAutomationLabelUnset():
            return
        case QueueAutomationSourceUnreadable(detail):
            _LOG.warning(
                "The automation label admitted nothing: the tracker could not "
                "be read (%s).",
                detail,
                extra={
                    "event": "queue_label_admission_source_unreadable",
                    "detail": detail,
                },
            )
        case QueueLabelAdmissionsDecided(admitted, declined):
            for decision in declined:
                _LOG.info(
                    "The automation label did not admit queue item %s (%s).",
                    decision.item_id.value,
                    _queue_label_admission_declined_reason(decision.outcome),
                    extra={
                        "event": "queue_label_admission_declined",
                        "item_id": decision.item_id.value,
                        "outcome": type(decision.outcome).__name__,
                    },
                )
            if admitted or declined:
                _LOG.info(
                    "Automation-label admission sweep: %d admitted, %d declined.",
                    len(admitted),
                    len(declined),
                    extra={
                        "event": "queue_label_admission_swept",
                        "admitted": len(admitted),
                        "declined": len(declined),
                    },
                )
        case _ as unreachable:
            assert_never(unreachable)


def _dbos_config(settings: DbosRuntimeSettings, engine: Engine) -> DBOSConfig:
    return {
        "name": "atelier2",
        "system_database_url": sqlite_url(settings.database_path),
        "system_database_engine": engine,
        "application_version": settings.application_version,
        "executor_id": EXECUTOR_ID,
        "use_listen_notify": False,
        "notification_listener_polling_interval_sec": 0.01,
    }


def _register_queues() -> None:
    """The run queue this launched runtime polls, admitting as much as there
    are workers.

    Polled rather than notified, because this deployment runs without
    LISTEN/NOTIFY, and polled often enough that a freed place is taken without an
    operator-visible pause. Registering on every launch is deliberate: the
    configuration lives in the system database, and this is the process that owns
    what it should say.
    """

    DBOS.register_queue(
        QUEUE_NAME,
        polling_interval_sec=0.05,
        on_conflict="always_update",
    )


class _DbosProcessOwner:
    """Owner of the one DBOS global, canonical engine, and workflow registry a
    process may hold.

    DBOS silently reuses its global singleton, so a second binding would adopt
    the first one's database and application version instead of failing. This
    owner refuses that before any global mutation and counts the leases that
    share the accepted binding, so recovery concurrency stays across processes.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._bound: _BoundRuntime | None = None

    def acquire(
        self,
        settings: DbosRuntimeSettings,
        agent_registry: AgentExecutorRegistry,
        effect_registry: EffectAdapterRegistry,
        *,
        tracker_item_source: TrackerItemSource | None = None,
    ) -> _BoundRuntime:
        with self._lock:
            agent_manifest = agent_registry.manifest
            effect_bindings = effect_registry.bindings
            requested_binding = settings.binding(agent_manifest, effect_bindings)
            if self._bound is None:
                self._bound = _open_binding(
                    settings,
                    agent_registry,
                    effect_registry,
                    effect_bindings,
                    tracker_item_source=tracker_item_source,
                )
            elif (
                self._bound.settings.binding(
                    self._bound.agent_executor_registry.manifest,
                    self._bound.effect_adapter_bindings,
                )
                != requested_binding
            ):
                raise DbosRuntimeBindingConflict(
                    "this process already owns "
                    f"{self._bound.settings.binding(self._bound.agent_executor_registry.manifest, self._bound.effect_adapter_bindings)}; "
                    f"refusing {requested_binding}"
                )
            self._bound.leases += 1
            return self._bound

    def release(self, bound: _BoundRuntime) -> None:
        with self._lock:
            bound.leases -= 1
            if bound.leases > 0:
                return
            try:
                errors: list[BaseException] = self._stopped_queue_sweep(bound)
                try:
                    DBOS.destroy(
                        destroy_registry=True,
                        workflow_completion_timeout_sec=(
                            _SHUTDOWN_WORKFLOW_COMPLETION_SECONDS
                            if bound.launched
                            else 0
                        ),
                    )
                except BaseException as error:
                    errors.append(error)
                errors.extend(
                    _cleanup_binding_resources(
                        bound.engine,
                        list(bound.agent_executors_v2),
                        bound.effect_adapters,
                        bound.agent_process_supervisor,
                        bound.agent_workspace_owner,
                    )
                )
            finally:
                self._bound = None
            if len(errors) == 1:
                raise errors[0]
            if errors:
                raise BaseExceptionGroup("runtime close failed", errors)

    @staticmethod
    def _stopped_queue_sweep(bound: _BoundRuntime) -> list[BaseException]:
        """Stop the sweep's clock before the binding it sweeps through goes.

        Answers with what failed rather than raising it: a close collects
        every independently failing step and reports them together.
        """

        ticker = bound.queue_sweep
        if ticker is None:
            return []
        bound.queue_sweep = None
        try:
            ticker.stop()
        except BaseException as error:
            return [error]
        return []

    def launch(self, bound: _BoundRuntime) -> None:
        with self._lock:
            if bound.launched:
                return
            self._start(bound, retag_continuations=True)
            bound.launched = True
            self._converge_driverless_attempts(bound)
            self._converge_driverless_effect_intents(bound)
            self._converge_uncontinuable_runs(bound)
            self._close_terminal_claim_checkouts(bound)
            self._advance_queue(bound)
            self._start_queue_sweep(bound)

    @staticmethod
    @staticmethod
    def _converge_driverless_attempts(bound: _BoundRuntime) -> None:
        """Answer for what the last process left armed, once recovery is armed.

        After the launch, and not before: the launch is what replays the
        workflows that are still pending, so asking first would stop attempts
        that recovery was about to drive. An attempt only exists where a scratch
        root is declared -- a V2 agent node refuses before it prepares one
        otherwise -- so a runtime without a workspace owner has none to converge.
        """

        supervisor = bound.agent_process_supervisor
        workspaces = bound.agent_workspace_owner
        if supervisor is None or workspaces is None:
            return
        converge_driverless_attempts(
            DbosAgentAttemptStore(bound.engine, bound.settings.application_version),
            supervisor,
            workspaces,
        )

    @staticmethod
    def _converge_driverless_effect_intents(bound: _BoundRuntime) -> None:
        """Route effect intents whose durable workflow raised to the operator.

        After the launch, for the same reason as the attempt sweep: only once
        recovery has re-armed every pending workflow does a terminal
        workflow_status row mean nothing will resolve the intent. Before the
        uncontinuable-run sweep: routing lifts a stranded action run to
        WAITING_RECONCILIATION, out of the STARTED rows that inventory reads,
        so an effect nobody observed reaches the operator door instead of
        being misread as a dead gap.
        """

        converge_driverless_effect_intents(
            bound.engine, bound.settings.application_version
        )

    @staticmethod
    def _converge_uncontinuable_runs(bound: _BoundRuntime) -> None:
        """End STARTED runs whose current node can no longer continue.

        After driverless-attempt convergence: that path stops armed attempts
        whose driver died and leaves them INTERRUPTED. This path is the
        leftover half — the attempt is already FAILED or INTERRUPTED, or the
        run advanced onto a node that never prepared and whose durable
        workflow will not recover, the run still says STARTED, and nothing
        will move it.
        """

        converge_uncontinuable_runs(
            DbosUncontinuableRunStore(bound.engine, bound.settings.application_version)
        )

    @staticmethod
    def _close_terminal_claim_checkouts(bound: _BoundRuntime) -> None:
        """Remove leftover claim checkouts of runs the store already ended.

        Walks only the checkouts this adapter currently holds under its own
        root, never the run history. A close that cannot finish leaves the
        run as it stands; the next tick tries again.
        """

        ledger = bound.work_item_claims
        if ledger is None:
            return
        checkouts = ledger.checkouts
        if not isinstance(checkouts, LocalClaimCheckouts):
            return
        try:
            close_checkouts_of_terminal_runs(
                bound.engine, checkouts, checkouts.standing_run_ids()
            )
        except Exception:
            _LOG.exception(
                "Closing leftover claim checkouts failed; the next tick asks again.",
                extra={"event": "claim_checkout_close_failed"},
            )

    @staticmethod
    def _advance_queue(bound: _BoundRuntime) -> None:
        """Admit what the automation label names, then start each launch once.

        Admission first: an item the label admits in this sweep is one the
        same sweep can start, rather than one waiting for the next process
        start. Without a served project or a connected tracker there is no
        policy to read and no label to read it against, so only the start half
        runs.
        """

        # Local import: `starter` imports `DbosRuntimeSettings` from this module,
        # so importing it at module scope would close a cycle.
        from atelier2.adapters.dbos.starter import DbosDurableRunStarter

        queue = DbosQueueProjectionStore(bound.engine)
        project = bound.settings.project_id
        tracker = bound.tracker_item_source
        if project is not None and tracker is not None:
            _log_queue_label_admission(
                admit_queue_items_by_label(queue, project=project, tracker=tracker)
            )
        advance_queue(
            queue,
            DbosCatalogStore(bound.engine),
            DbosDurableRunStarter(
                bound.engine,
                bound.settings,
                bound.agent_executor_registry,
            ),
            workflow_document_parser=parse_workflow_document,
            served_project=project,
            tracker=tracker,
        )

    def _start_queue_sweep(self, bound: _BoundRuntime) -> None:
        """Give the sweep its clock, now that one sweep has already run.

        The tick is what starts an item admitted after this launch: without it
        the queue would stand still until the next deploy, however long that
        is. An admission does not wait for the next tick either -- the door
        asks the same ticker for a sweep at once.
        """

        ticker = QueueSweepTicker(partial(self._swept_queue, bound))
        bound.queue_sweep = ticker
        ticker.start()

    @staticmethod
    def _swept_queue(bound: _BoundRuntime) -> None:
        """One tick's sweep, whose failure ends that turn and not the tick.

        An unreadable queue and an unavailable write are exactly what the next
        tick asks about again; a tick that died here would leave the queue
        silent until the next deploy, which is the state the tick exists to
        end. The launch's own sweep still raises, because a process that
        cannot sweep at all should not come up quietly.
        """

        _DbosProcessOwner._close_terminal_claim_checkouts(bound)
        try:
            _DbosProcessOwner._advance_queue(bound)
        except Exception:
            _LOG.exception(
                "The queue sweep failed; the next tick asks again.",
                extra={"event": "queue_sweep_failed"},
            )

    def initialize_storage(self, bound: _BoundRuntime) -> None:
        with self._lock:
            if bound.storage_ready:
                return
            self._start(bound, retag_continuations=False)
            DBOS.destroy()

    @staticmethod
    def _start(bound: _BoundRuntime, *, retag_continuations: bool) -> None:
        DBOS(config=_dbos_config(bound.settings, bound.engine))
        if retag_continuations:
            retag_stranded_continuations(
                bound.engine, bound.settings.application_version
            )
        DBOS.launch()
        _register_queues()
        bound.storage_ready = True


_PROCESS_OWNER = _DbosProcessOwner()


@dataclass(frozen=True)
class _FilesystemProviderProbeReceiptReads:
    """Reads a live provider probe receipt by the configuration it proves.

    `host/provider_canary.py` files each receipt under its own vector id, not
    under the configuration hash a caller here asks about, so this scans the
    small, fixed-size receipt directory instead of trusting a filename. An
    unreadable directory, an unreadable file, or bytes that do not parse as a
    receipt all answer "no receipt for this configuration" rather than
    raising: absent or corrupt evidence is exactly what an armed gate already
    refuses, and one bad file must not blind every other vector's own answer.
    Each such case is logged once, though, because "every vector reads
    unstartable" is exactly what an operator needs a trace to diagnose --
    fail-closed does not have to mean fail-silent.
    """

    directory: Path

    def receipt_for(
        self, configuration_hash: AgentConfigurationRevisionHash
    ) -> ProviderProbeReceipt | None:
        try:
            entries = tuple(self.directory.glob("*.json"))
        except OSError as unreadable:
            _LOG.warning(
                "provider probe receipt directory %s unreadable: %s",
                self.directory,
                unreadable,
            )
            return None
        for entry in entries:
            try:
                document = entry.read_bytes()
            except OSError as unreadable:
                _LOG.warning(
                    "provider probe receipt file %s unreadable: %s",
                    entry,
                    unreadable,
                )
                continue
            receipt = read_provider_probe_receipt(document)
            if isinstance(receipt, ProviderProbeReceiptRefused):
                _LOG.warning(
                    "provider probe receipt file %s is not a valid receipt: %s",
                    entry,
                    receipt,
                )
                continue
            if receipt.configuration_hash == configuration_hash:
                return receipt
        return None


def _resolve_admitted_canary_revisions(
    engine: Engine,
) -> frozenset[WorkflowRevisionHash]:
    """The live-admitted head revision of every currently named canary workflow.

    Asked fresh on every reprobe exemption check, not cached: an unresolved or
    retired name simply contributes no hash, so a misconfigured or empty
    catalog answers the empty set -- the exemption's own structural default,
    not a special case handled here. `PROVIDER_CANARY_WORKFLOW_NAMES` is the
    one production list of catalog names a canary vector may resolve under
    (`contracts/provider_probe_receipts.py`, read by `host/provider_canary.py`
    too); resolution itself reuses `DbosCatalogStore.resolve_name`, the same
    lookup `GET /catalog-revisions/by-name/{kind}/{name}` answers from, not a second
    mechanism.
    """

    store = DbosCatalogStore(engine)
    resolved: set[WorkflowRevisionHash] = set()
    for name in PROVIDER_CANARY_WORKFLOW_NAMES:
        found = store.resolve_name(
            RevisionKind.WORKFLOW, CatalogLineageDisplayName(name), "head"
        )
        if isinstance(found, CatalogNameFound) and not found.retired:
            resolved.add(WorkflowRevisionHash(found.revision_hash.value))
    return frozenset(resolved)


def _receipt_gate(settings: DbosRuntimeSettings) -> ProviderProbeReceiptGate | None:
    if (
        settings.provider_probe_receipt_directory is None
        or settings.provider_probe_receipt_provider_layer_digest is None
    ):
        return None
    return ProviderProbeReceiptGate(
        _FilesystemProviderProbeReceiptReads(settings.provider_probe_receipt_directory),
        settings.provider_probe_receipt_provider_layer_digest,
        recorded_instant,
    )


class DbosRuntime:
    """One lease on the process-global DBOS runtime binding.

    Closing releases that lease exactly once, so concurrent closes of one lease
    cannot destroy a binding another lease still holds.
    """

    def __init__(
        self,
        settings: DbosRuntimeSettings,
        effect_adapter_factory: EffectAdapterFactory | EffectAdapterRegistry,
        agent_executor_factories_v2: tuple[
            AgentExecutorFactoryV2 | AgentExecutorRegistration, ...
        ] = (),
        *,
        tracker_item_source: TrackerItemSource | None = None,
    ) -> None:
        self._close_lock = threading.Lock()
        registry = AgentExecutorRegistry(
            agent_executor_factories_v2,
            receipt_gate=_receipt_gate(settings),
            reprobe_exempt_workflow_revisions=self._reprobe_exempt_workflow_revisions,
        )
        effect_registry = (
            effect_adapter_factory
            if isinstance(effect_adapter_factory, EffectAdapterRegistry)
            else EffectAdapterRegistry(
                (
                    EffectAdapterRegistration(
                        effect_adapter_factory.binding.operation_name,
                        effect_adapter_factory,
                    ),
                )
            )
        )
        self._bound: _BoundRuntime | None = _PROCESS_OWNER.acquire(
            settings, registry, effect_registry, tracker_item_source=tracker_item_source
        )

    @property
    def settings(self) -> DbosRuntimeSettings:
        return self._held().settings

    @property
    def engine(self) -> Engine:
        return self._held().engine

    def _reprobe_exempt_workflow_revisions(self) -> frozenset[WorkflowRevisionHash]:
        """The registry's own exemption source, bound but not yet callable.

        Passed into `AgentExecutorRegistry` before `self._bound` exists --
        that registry is what `_PROCESS_OWNER.acquire` below needs to open the
        binding in the first place. Safe anyway: nothing calls this bound
        method until a real start reaches the exemption check, and by then
        `self._bound` is set and `self.engine` answers.
        """

        return _resolve_admitted_canary_revisions(self.engine)

    @property
    def datasource(self) -> SQLAlchemyDatasource:
        return self._held().datasource

    @property
    def effect_adapter(self) -> EffectAdapter:
        binding = self.effect_adapter_binding
        return self._held().effect_adapters.adapter_for(binding.operation_name, binding)

    @property
    def agent_executor_registry(self) -> AgentExecutorRegistry:
        return self._held().agent_executor_registry

    @property
    def agent_process_supervisor(self) -> AgentProcessSupervisor:
        supervisor = self._held().agent_process_supervisor
        if supervisor is None:
            raise AgentProcessSupervisorUnavailable(
                "runtime has no local agent process supervisor: no LOCAL_PROCESS-"
                "carried executor key is registered"
            )
        return supervisor

    @property
    def agent_workspace_owner(self) -> LocalAgentAttemptWorkspaceOwner | None:
        return self._held().agent_workspace_owner

    @property
    def declared_project(self) -> DeclaredProject | None:
        return self._held().declared_project

    @property
    def effect_adapter_binding(self) -> EffectAdapterBinding:
        bindings = self._held().effect_adapter_bindings
        for binding in bindings:
            if binding.operation_name is AdapterOperationName.OPEN_PR:
                return binding
        return bindings[0]

    def launch(self) -> None:
        _PROCESS_OWNER.launch(self._held())

    def request_queue_sweep(self) -> None:
        """Sweep the queue now rather than at the next tick.

        Silent once this lease is closed: a sweep needs the process the lease
        held, and whatever admission asked for one is already durable.
        """

        bound = self._bound
        if bound is not None and bound.queue_sweep is not None:
            bound.queue_sweep.sweep_now()

    def initialize_storage(self) -> None:
        _PROCESS_OWNER.initialize_storage(self._held())

    def close(self) -> None:
        with self._close_lock:
            bound = self._bound
            if bound is None:
                return
            self._bound = None
            _PROCESS_OWNER.release(bound)

    def _held(self) -> _BoundRuntime:
        if self._bound is None:
            raise DbosRuntimeLeaseClosed("this DBOS runtime lease is already closed")
        return self._bound
