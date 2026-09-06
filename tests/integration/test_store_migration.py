"""The offline hop a live V13 store needed and did not have.

The V13 fixture is a real predecessor store: the current create path, every later
addition removed, then a format-3 run that already wrote one event. That is the
#240 Z2 method — predecessor schema, not a version-row stub — expressed through
today's owner.

V14 and V15 each added a table, so dropping those tables was the whole reversal.
Every version after them instead reshapes a table V13 already had -- V21 is the
capability CHECK on `agent_configuration_revisions` -- so the fixture also
restores each of those tables' published V13 shape below. The literals are not
second owners of the current tables: they are the frozen artifacts V13 really
carried, and the pinned V13 fingerprint refuses them the moment a character
drifts.
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from multiprocessing import get_context
from pathlib import Path

import pytest
import sqlalchemy as sa
from dbos import DBOSClient, EnqueueOptions
from dbos._serialization import DefaultSerializer
from sqlalchemy.engine import Connection

from atelier2.adapters.dbos import schema as schema_module
from atelier2.adapters.dbos.agent_attempt_store import DbosAgentAttemptStore
from atelier2.adapters.dbos.host_configuration import (
    project_source_connection_revision_from_record,
)
from atelier2.adapters.dbos.names import ANSWER_WORKFLOW_NAME, QUEUE_NAME
from atelier2.adapters.dbos.published_schema_shapes import (
    PUBLISHED_QUEUE_ITEMS_STATE_TRANSITION_TRIGGER_BEFORE_OBSERVATION,
    PUBLISHED_TABLE_SHAPES,
)
from atelier2.adapters.dbos.run_store import (
    DbosWaitAnswerer,
    _agent_receipt_v2_from_record,
    _agent_receipt_v2_values,
    _tool_redemption_from_record,
    _tool_redemption_values,
)
from atelier2.adapters.dbos.run_transitions import event_from_record
from atelier2.adapters.dbos.runtime import create_canonical_engine
from atelier2.adapters.dbos.schema import (
    _AGENT_ATTEMPTS_TRIGGERS,
    _EFFECT_INTENTS_ABANDONMENT_TRIGGERS,
    _EFFECT_INTENTS_TRIGGERS,
    _OCCUPANCY_TRIGGER_STATEMENTS,
    _PREDECESSOR_ATTEMPTS_BEFORE_THE_TRANSCRIPT,
    _PREDECESSOR_INTENTS_BEFORE_ABANDONMENT,
    _PREDECESSOR_WAIT_ANSWERS,
    _PREDECESSOR_WAIT_UNCANCELLABLE_RUN_EVENTS,
    _PRODUCT_TRIGGERS,
    _ROUND_SCOPED_EVENT_INDEX,
    _RUN_EVENTS_TRIGGERS,
    _TOOL_REDEMPTIONS_TRIGGERS,
    _V17_AGENT_ATTEMPT_TRIGGERS,
    _V23_AGENT_ATTEMPT_TRIGGERS,
    _V24_AGENT_ATTEMPT_TRIGGERS,
    _V27_AGENT_ATTEMPT_STATE_TRANSITION,
    _V32_AGENT_ATTEMPT_TRIGGERS,
    _V38_AGENT_ATTEMPT_TRIGGERS,
    _V41_EFFECT_INTENT_TRIGGERS,
    _VERSION_TWENTY,
    _WAIT_ANSWERS_TRIGGERS,
    PRODUCT_SCHEMA_HANDOFF,
    SCHEMA_VERSION,
    V13_SCHEMA_HANDOFF,
    V21_SCHEMA_HANDOFF,
    V22_SCHEMA_HANDOFF,
    V23_SCHEMA_HANDOFF,
    V24_SCHEMA_HANDOFF,
    V25_SCHEMA_HANDOFF,
    V26_SCHEMA_HANDOFF,
    V27_SCHEMA_HANDOFF,
    V28_SCHEMA_HANDOFF,
    V29_SCHEMA_HANDOFF,
    V31_SCHEMA_HANDOFF,
    V32_SCHEMA_HANDOFF,
    V33_SCHEMA_HANDOFF,
    V34_SCHEMA_HANDOFF,
    V35_SCHEMA_HANDOFF,
    V36_SCHEMA_HANDOFF,
    V37_SCHEMA_HANDOFF,
    V38_SCHEMA_HANDOFF,
    V39_SCHEMA_HANDOFF,
    V40_SCHEMA_HANDOFF,
    V41_SCHEMA_HANDOFF,
    V42_SCHEMA_HANDOFF,
    V43_SCHEMA_HANDOFF,
    V44_SCHEMA_HANDOFF,
    V45_SCHEMA_HANDOFF,
    V49_SCHEMA_HANDOFF,
    MigrationRequired,
    StoreMigrationRefused,
    _rebuild_product_table,
    _refuse_redemptions_that_cannot_be_re_owned,
    _require_product_shape,
    agent_attempts,
    agent_configuration_revisions,
    agent_receipts_v2,
    artifacts,
    atelier_schema_versions,
    attempt_instants,
    auth_profile_revisions,
    catalog_lineage_members,
    catalog_lineages,
    context_packages_v3,
    effect_intents,
    effect_receipts,
    event_instants,
    host_model_registry_entries,
    host_model_registry_revisions,
    host_project_model_defaults,
    host_project_model_defaults_revisions,
    host_project_root_revisions,
    host_project_source_connection_revisions,
    initialize_schema,
    migrate_store,
    node_execution_requests_v3,
    node_receipts_v3,
    published_revisions,
    queue_items,
    queue_project_policy_revisions,
    queue_proposal_revisions,
    run_agent_bindings,
    run_configuration_revisions,
    run_events,
    run_fork_effect_fences,
    run_fork_reused_nodes,
    run_forks,
    run_inputs_v3,
    run_instants,
    runs,
    tool_redemptions,
    wait_answers,
    webhook_delivery_cursor,
    workflow_revisions,
)
from atelier2.adapters.dbos.workflow_ids import answer_workflow_id_for
from atelier2.api.references import encode_public_run_reference

host_occupancy_revisions = sa.table("host_occupancy_revisions")
host_occupancy_bindings = sa.table("host_occupancy_bindings")
from atelier2.application.answer_wait import (
    AnswerAcceptedPending,
    answer_wait_result,
)
from atelier2.contracts.adapter_operations_v3 import AdapterOperationName
from atelier2.contracts.agent_attempts import (
    AGENT_ATTEMPT_ORDINAL,
    REPLACEMENT_AGENT_ATTEMPT_ORDINAL,
    AgentAttemptCancellationDisposition,
    AgentAttemptFailureCode,
    AgentAttemptId,
    AgentAttemptReplacement,
    AgentAttemptState,
)
from atelier2.contracts.agent_transcripts import AssistantTurn, AttemptTranscript
from atelier2.contracts.agents import (
    AgentBinding,
    AgentBindingSet,
    AgentConfigurationRevision,
    AgentConfigurationRevisionFormatVersion,
    AgentExecutionCapability,
    AgentExecutionRequestHash,
    AgentExecutionRequestV2,
    AgentExecutionResult,
    AgentExecutorOperationalIdentity,
    AgentExecutorRevision,
    AgentReceiptHash,
    AgentReceiptV2,
    AgentRole,
    AuthMode,
    AuthProfileRevision,
    ProviderId,
    ResolvedAgentBinding,
)
from atelier2.contracts.artifacts import Artifact
from atelier2.contracts.catalog_v3 import CatalogLineage
from atelier2.contracts.effects import (
    EFFECT_INTENT_VERSION_ABANDONED,
    EFFECT_INTENT_VERSION_CONFIRMED_INITIAL,
    EFFECT_INTENT_VERSION_INITIAL,
    EFFECT_INTENT_VERSION_WAITING,
    ConfirmationSource,
    EffectIntentState,
    LogicalEffectKey,
)
from atelier2.contracts.executions import (
    NodeExecutionId,
    RunEvent,
    RunEventAgentAttemptBinding,
    RunEventCancellationBinding,
    RunEventKind,
    WaitAnswerActor,
    WaitAnswerState,
)
from atelier2.contracts.hashing import Sha256Hash
from atelier2.contracts.host_configuration import (
    ConnectionActor,
    ProjectId,
    ProjectRootRevision,
    SourceAddress,
    SourceConnectionAuthMethod,
    SourceKind,
)
from atelier2.contracts.queue_projection import (
    QueueAutomationDisposition,
    QueueItemState,
    QueueProposalSource,
    TrackerItemReference,
    WorkItemReference,
)
from atelier2.contracts.revisions_v3 import (
    PublishedRevision,
    PublishedRevisionHash,
    RevisionKind,
)
from atelier2.contracts.run_cancellations import RunCancelCommandId
from atelier2.contracts.runs import (
    FIRST_ROUND_ORDINAL,
    RunId,
    RunState,
    WorkflowRevision,
    WorkflowRevisionHash,
)
from atelier2.contracts.tool_grants_v3 import (
    DeclaredToolGrant,
    ToolGrantCapability,
    ToolRedemptionReceipt,
)
from atelier2.host import main
from atelier2.ports.run_queries import RunFound
from tests.integration.test_agent_attempts import attempt_request, attempt_runtime
from tests.integration.test_v3_wait_in_loop import public_client
from tests.integration.test_v3_wait_run import (
    ANSWER,
    RUN,
    WAIT_IN_THE_MIDDLE,
    WAIT_NODE,
    recording_provider,
    start_and_launch,
    wait_for_state,
    wait_runtime_over,
)
from tests.scenarios.agents import agent_attempt_execution
from tests.scenarios.api import durable_queries

ARCHIVED_RUN_ID = "live/erster-lauf-nach-der-nacht"
ARCHIVED_NODE_ID = "cook"
ARCHIVED_OUTPUT = b"lasagne, aufgetragen"

_V27_ACCESS_STORE_DDL = """
CREATE TABLE node_receipt_access_v3 (
	node_execution_id TEXT NOT NULL, position INTEGER NOT NULL, access_receipt_hash TEXT NOT NULL,
	PRIMARY KEY (node_execution_id, position), CHECK (position >= 0),
	CHECK (length(node_execution_id) = 64 AND node_execution_id NOT GLOB '*[^0-9a-f]*'), CHECK (length(access_receipt_hash) = 64 AND access_receipt_hash NOT GLOB '*[^0-9a-f]*'),
	FOREIGN KEY(node_execution_id) REFERENCES node_receipts_v3 (node_execution_id)
);
CREATE TRIGGER node_receipt_access_v3_no_update BEFORE UPDATE ON node_receipt_access_v3 BEGIN SELECT RAISE(ABORT, 'v3 node receipt access is immutable'); END; CREATE TRIGGER node_receipt_access_v3_no_delete BEFORE DELETE ON node_receipt_access_v3 BEGIN SELECT RAISE(ABORT, 'v3 node receipt access is immutable'); END;
"""


def _restore_v27_access_store(connection: sqlite3.Connection) -> None:
    exists = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' "
        "AND name='node_receipt_access_v3'"
    ).fetchone()
    if exists is None:
        connection.executescript(_V27_ACCESS_STORE_DDL)


_PREDECESSOR_RUN_EVENTS_DDL = """
CREATE TABLE run_events (
    run_id TEXT NOT NULL,
    revision_hash TEXT NOT NULL,
    event_sequence INTEGER NOT NULL,
    node_id TEXT NOT NULL,
    node_execution_id TEXT NOT NULL,
    event_kind TEXT NOT NULL,
    payload BLOB NOT NULL,
    payload_hash TEXT NOT NULL,
    receipt_logical_key TEXT,
    receipt_result_hash TEXT,
    event_hash TEXT NOT NULL,
    agent_attempt_id TEXT,
    attempt_ordinal INTEGER,
    cancellation_command_id TEXT,
    replacement TEXT,
    cancellation_disposition TEXT,
    replacement_attempt_id TEXT,
    PRIMARY KEY (run_id, event_sequence),
    FOREIGN KEY(run_id, revision_hash) REFERENCES runs (run_id, revision_hash),
    FOREIGN KEY(receipt_logical_key, run_id, revision_hash, receipt_result_hash) REFERENCES effect_receipts (logical_key, run_id, workflow_revision_hash, result_hash),
    CHECK (event_sequence > 0),
    CHECK (length(node_id) > 0),
    CHECK (length(node_execution_id) = 64 AND node_execution_id NOT GLOB '*[^0-9a-f]*'),
    CHECK (event_kind IN ('AGENT_COMPLETED', 'AGENT_FAILED', 'AGENT_CANCEL_REQUESTED', 'AGENT_CANCELLED', 'AGENT_INTERRUPTED', 'ACTION_RECONCILIATION_REQUIRED', 'ACTION_RECONCILIATION_RESOLVED', 'ACTION_COMPLETED', 'WAITING_INPUT', 'WAIT_ANSWERED', 'SUBWORKFLOW_COMPLETED')),
    CHECK (length(payload_hash) = 64 AND payload_hash NOT GLOB '*[^0-9a-f]*'),
    CHECK (length(event_hash) = 64 AND event_hash NOT GLOB '*[^0-9a-f]*'),
    CHECK ((event_kind IN ('ACTION_RECONCILIATION_RESOLVED', 'ACTION_COMPLETED') AND receipt_logical_key IS NOT NULL AND length(receipt_logical_key) > 0 AND receipt_result_hash IS NOT NULL AND length(receipt_result_hash) = 64 AND receipt_result_hash NOT GLOB '*[^0-9a-f]*' AND receipt_result_hash = payload_hash) OR (event_kind NOT IN ('ACTION_RECONCILIATION_RESOLVED', 'ACTION_COMPLETED') AND receipt_logical_key IS NULL AND receipt_result_hash IS NULL)),
    CHECK ((agent_attempt_id IS NULL AND attempt_ordinal IS NULL AND cancellation_command_id IS NULL AND replacement IS NULL AND cancellation_disposition IS NULL AND replacement_attempt_id IS NULL) OR (length(agent_attempt_id) = 64 AND agent_attempt_id NOT GLOB '*[^0-9a-f]*' AND attempt_ordinal IN (1, 2) AND ((event_kind IN ('AGENT_COMPLETED', 'AGENT_FAILED') AND cancellation_command_id IS NULL AND replacement IS NULL AND cancellation_disposition IS NULL AND replacement_attempt_id IS NULL) OR (event_kind = 'AGENT_CANCEL_REQUESTED' AND length(cancellation_command_id) BETWEEN 1 AND 1024 AND replacement IN ('NONE', 'ONE') AND cancellation_disposition IS NULL AND replacement_attempt_id IS NULL) OR (event_kind IN ('AGENT_CANCELLED', 'AGENT_INTERRUPTED') AND length(cancellation_command_id) BETWEEN 1 AND 1024 AND replacement IN ('NONE', 'ONE') AND cancellation_disposition IS NOT NULL))))
)
"""


_PREDECESSOR_AGENT_ATTEMPTS_DDL = """
CREATE TABLE agent_attempts (
    attempt_id TEXT NOT NULL,
    node_execution_id TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    executor_operational_identity TEXT NOT NULL,
    run_id TEXT NOT NULL,
    workflow_revision_hash TEXT NOT NULL,
    node_id TEXT NOT NULL,
    attempt_ordinal INTEGER NOT NULL,
    state TEXT NOT NULL,
    state_version INTEGER NOT NULL,
    process_phase TEXT NOT NULL,
    process_owner_id TEXT,
    watchdog_generation_id TEXT,
    cancellation_command_id TEXT,
    cancellation_expected_state_version INTEGER,
    replacement TEXT,
    redrive_state TEXT,
    cancellation_disposition TEXT,
    cancellation_workflow_id TEXT,
    failure_code TEXT,
    receipt_hash TEXT,
    PRIMARY KEY (attempt_id),
    UNIQUE (node_execution_id, attempt_ordinal),
    FOREIGN KEY(run_id, workflow_revision_hash) REFERENCES runs (run_id, revision_hash),
    CHECK (length(attempt_id) = 64 AND attempt_id NOT GLOB '*[^0-9a-f]*'),
    CHECK (length(node_execution_id) = 64 AND node_execution_id NOT GLOB '*[^0-9a-f]*'),
    CHECK (length(request_hash) = 64 AND request_hash NOT GLOB '*[^0-9a-f]*'),
    CHECK (length(executor_operational_identity) BETWEEN 1 AND 1024),
    CHECK (length(run_id) > 0),
    CHECK (length(workflow_revision_hash) = 64 AND workflow_revision_hash NOT GLOB '*[^0-9a-f]*'),
    CHECK (length(node_id) BETWEEN 1 AND 1024),
    CHECK (attempt_ordinal IN (1, 2)),
    CHECK (process_phase IN ('NONE', 'WATCHDOG_READY', 'LAUNCH_AUTHORIZED', 'PROCESS_OBSERVED', 'CLEANUP_ATTESTED')),
    CHECK ((process_phase = 'NONE' AND process_owner_id IS NULL AND watchdog_generation_id IS NULL) OR (process_phase = 'CLEANUP_ATTESTED' AND cancellation_disposition = 'NEVER_LAUNCHED' AND process_owner_id IS NULL AND watchdog_generation_id IS NULL) OR (process_phase <> 'NONE' AND length(process_owner_id) BETWEEN 1 AND 1024 AND length(watchdog_generation_id) BETWEEN 1 AND 1024)),
    CHECK ((cancellation_command_id IS NULL AND cancellation_expected_state_version IS NULL AND replacement IS NULL AND redrive_state IS NULL AND cancellation_disposition IS NULL AND cancellation_workflow_id IS NULL) OR (length(cancellation_command_id) BETWEEN 1 AND 1024 AND cancellation_expected_state_version >= 0 AND replacement IN ('NONE', 'ONE') AND redrive_state IN ('PENDING', 'OWNER_NOT_LOCAL', 'CLEANUP_ATTESTED') AND length(cancellation_workflow_id) > 0 AND ((redrive_state = 'CLEANUP_ATTESTED' AND cancellation_disposition IN ('NEVER_LAUNCHED', 'EXITED_BEFORE_SIGNAL', 'REAPED_AFTER_TERM', 'REAPED_AFTER_KILL', 'OWNER_LOST_AFTER_PARENT_DEATH')) OR (redrive_state <> 'CLEANUP_ATTESTED' AND cancellation_disposition IS NULL)))),
    CHECK ((state = 'PREPARED' AND state_version = 0 AND process_phase = 'NONE' AND cancellation_command_id IS NULL AND failure_code IS NULL AND receipt_hash IS NULL) OR (state = 'PREPARED' AND state_version = 1 AND process_phase = 'WATCHDOG_READY' AND cancellation_command_id IS NULL AND failure_code IS NULL AND receipt_hash IS NULL) OR (state = 'LAUNCH_ARMED' AND state_version = 1 AND process_phase IN ('NONE', 'LAUNCH_AUTHORIZED') AND cancellation_command_id IS NULL AND failure_code IS NULL AND receipt_hash IS NULL) OR (state = 'LAUNCH_ARMED' AND state_version >= 2 AND process_phase IN ('LAUNCH_AUTHORIZED', 'PROCESS_OBSERVED') AND cancellation_command_id IS NULL AND failure_code IS NULL AND receipt_hash IS NULL) OR (state = 'CANCEL_REQUESTED' AND state_version >= 1 AND cancellation_command_id IS NOT NULL AND cancellation_disposition IS NULL AND failure_code IS NULL AND receipt_hash IS NULL) OR (state IN ('CANCELLED', 'INTERRUPTED') AND state_version >= 2 AND process_phase = 'CLEANUP_ATTESTED' AND cancellation_command_id IS NOT NULL AND cancellation_disposition IS NOT NULL AND failure_code IS NULL AND receipt_hash IS NULL) OR (state = 'SUCCEEDED' AND state_version >= 2 AND cancellation_command_id IS NULL AND failure_code IS NULL AND receipt_hash IS NOT NULL) OR (state = 'FAILED' AND state_version >= 2 AND cancellation_command_id IS NULL AND failure_code = 'PROCESS_EXITED_UNSUCCESSFULLY' AND receipt_hash IS NULL)),
    UNIQUE (cancellation_workflow_id),
    UNIQUE (receipt_hash),
    FOREIGN KEY(receipt_hash) REFERENCES agent_receipts_v2 (receipt_hash) ON DELETE RESTRICT
)
"""

_PREDECESSOR_AGENT_ATTEMPTS_TRIGGER_DDL = """
CREATE TRIGGER agent_attempts_state_transition
BEFORE UPDATE ON agent_attempts
WHEN NOT (
  OLD.attempt_id = NEW.attempt_id
  AND OLD.node_execution_id = NEW.node_execution_id
  AND OLD.request_hash = NEW.request_hash
  AND OLD.executor_operational_identity = NEW.executor_operational_identity
  AND OLD.run_id = NEW.run_id
  AND OLD.workflow_revision_hash = NEW.workflow_revision_hash
  AND OLD.node_id = NEW.node_id
  AND OLD.attempt_ordinal = NEW.attempt_ordinal
  AND NEW.state_version > OLD.state_version
  AND (
    (OLD.state = 'PREPARED' AND OLD.state_version = 0
     AND OLD.failure_code IS NULL AND OLD.receipt_hash IS NULL
     AND NEW.state = 'PREPARED' AND NEW.state_version = 1
     AND NEW.process_phase = 'WATCHDOG_READY'
     AND NEW.failure_code IS NULL AND NEW.receipt_hash IS NULL
     AND NEW.cancellation_command_id IS NULL)
    OR
    (OLD.state = 'PREPARED'
     AND NEW.state = 'LAUNCH_ARMED'
     AND NEW.process_phase IN ('NONE', 'LAUNCH_AUTHORIZED')
     AND NEW.failure_code IS NULL AND NEW.receipt_hash IS NULL
     AND NEW.cancellation_command_id IS NULL)
    OR
    (OLD.state = 'LAUNCH_ARMED'
     AND OLD.process_phase = 'LAUNCH_AUTHORIZED'
     AND NEW.state = 'LAUNCH_ARMED'
     AND NEW.process_phase = 'PROCESS_OBSERVED'
     AND NEW.failure_code IS NULL AND NEW.receipt_hash IS NULL
     AND NEW.cancellation_command_id IS NULL)
    OR
    (OLD.state = 'LAUNCH_ARMED'
     AND OLD.failure_code IS NULL AND OLD.receipt_hash IS NULL
     AND NEW.state = 'SUCCEEDED'
     AND NEW.failure_code IS NULL AND NEW.receipt_hash IS NOT NULL
     AND NEW.cancellation_command_id IS NULL
     AND EXISTS (
       SELECT 1 FROM agent_receipts_v2 AS receipt
       WHERE receipt.receipt_hash = NEW.receipt_hash
         AND receipt.request_hash = NEW.request_hash
         AND receipt.executor_operational_identity = NEW.executor_operational_identity
         AND receipt.node_execution_id = NEW.node_execution_id
         AND receipt.run_id = NEW.run_id
         AND receipt.workflow_revision_hash = NEW.workflow_revision_hash
         AND receipt.node_id = NEW.node_id
     ))
    OR
    (OLD.state = 'LAUNCH_ARMED'
     AND OLD.failure_code IS NULL AND OLD.receipt_hash IS NULL
     AND NEW.state = 'FAILED'
     AND NEW.failure_code = 'PROCESS_EXITED_UNSUCCESSFULLY'
     AND NEW.receipt_hash IS NULL
     AND NEW.cancellation_command_id IS NULL)
    OR
    (OLD.state IN ('PREPARED', 'LAUNCH_ARMED')
     AND OLD.cancellation_command_id IS NULL
     AND NEW.state = 'CANCEL_REQUESTED'
     AND NEW.cancellation_command_id IS NOT NULL
     AND NEW.cancellation_expected_state_version = OLD.state_version
     AND NEW.failure_code IS NULL AND NEW.receipt_hash IS NULL)
    OR
    (OLD.state = 'CANCEL_REQUESTED'
     AND NEW.state = 'CANCEL_REQUESTED'
     AND OLD.cancellation_command_id = NEW.cancellation_command_id
     AND NEW.redrive_state = 'OWNER_NOT_LOCAL'
     AND NEW.failure_code IS NULL AND NEW.receipt_hash IS NULL)
    OR
    (OLD.state = 'CANCEL_REQUESTED'
     AND NEW.state IN ('CANCELLED', 'INTERRUPTED')
     AND OLD.cancellation_command_id = NEW.cancellation_command_id
     AND NEW.process_phase = 'CLEANUP_ATTESTED'
     AND NEW.redrive_state = 'CLEANUP_ATTESTED'
     AND NEW.cancellation_disposition IS NOT NULL
     AND NEW.failure_code IS NULL AND NEW.receipt_hash IS NULL)
  )
) BEGIN
  SELECT RAISE(ABORT, 'invalid agent attempt transition');
END
"""

ARCHIVED_ATTEMPT_ID = "ab" * 32
ARCHIVED_ATTEMPT_FAILURE_CODE = "PROCESS_EXITED_UNSUCCESSFULLY"
ARCHIVED_RECEIPT_NODE_EXECUTION_ID = "99" * 32
ARCHIVED_AGENT_CONFIGURATION_HASH = "66" * 32
ARCHIVED_AGENT_MODEL = "archived-model"
"""The published configuration an old store already carried.

The capability hop rebuilds the table this row lives in, so the fixture's own
published binding is what proves the rows came over: a hop that rebuilt an empty
table would prove the shape and nothing about what stood in it.
"""


def _logical_dump(database_path: Path) -> tuple[str, ...]:
    with sqlite3.connect(database_path) as connection:
        return tuple(connection.iterdump())


def _crash_v44_migration_after_queue_copy(database_path: str) -> None:
    raise_declared_version = schema_module._raise_declared_version

    def exit_after_v44_copy(
        connection: sqlite3.Connection, expected_version: int, target_version: int
    ) -> None:
        raise_declared_version(connection, expected_version, target_version)
        if expected_version == 43:
            os._exit(79)

    schema_module._raise_declared_version = exit_after_v44_copy
    migrate_store(Path(database_path))


def _restore_v43_queue_predecessor(connection: sqlite3.Connection) -> None:
    """Remove Phase D and restore the admission-only row every V29–V43 held."""

    _restore_v44_connection_predecessor(connection)
    schema_module._rebuild_product_table(
        connection,
        schema_module.queue_items,
        "queue_items_v47",
        ("queue_items_identity_no_update", "queue_items_no_delete"),
        47,
        43,
    )
    for table in reversed(schema_module._PHASE_D_QUEUE_TABLES):
        connection.execute(f"DROP TABLE {table.name}")


def _restore_v44_connection_predecessor(connection: sqlite3.Connection) -> None:
    """Restore the project-source table shape published by V44."""

    _restore_v45_answer_attribution_predecessors(connection)
    schema_module._rebuild_product_table(
        connection,
        schema_module.host_project_source_connection_revisions,
        "project_source_connections_v45",
        (
            "host_project_source_connection_revisions_no_update",
            "host_project_source_connection_revisions_no_delete",
        ),
        45,
        44,
    )


def _restore_v51_queue_defaults_predecessor(connection: sqlite3.Connection) -> None:
    """Take back the policy defaults and the proposal source V52 added.

    Rebuilding both tables in the shape V51 published is the hop run the other
    way: every stored row keeps its remaining columns, and the columns V52
    introduced simply stop being. Past the vocabulary V53 widened, which a V51
    store predates just as a V52 one does.
    """

    _restore_v52_attempt_failure_vocabulary(connection)
    schema_module._rebuild_product_table(
        connection,
        schema_module.queue_proposal_revisions,
        "queue_proposal_revisions_v52",
        schema_module._QUEUE_PROPOSAL_TRIGGERS,
        52,
        51,
    )
    schema_module._rebuild_product_table(
        connection,
        schema_module.queue_project_policy_revisions,
        "queue_project_policy_revisions_v52",
        schema_module._QUEUE_POLICY_TRIGGERS,
        52,
        51,
    )


def _restore_v50_permission_ledger_predecessor(
    connection: sqlite3.Connection,
) -> None:
    """Take back the ledger V51 added, leaving every other table alone.

    V51 is additive, so a predecessor of it differs from today only by not
    carrying the ledger; dropping it is the whole restoration -- past the
    queue columns V52 added, which a V50 store predates just as a V51 one
    does.
    """

    _restore_v51_queue_defaults_predecessor(connection)
    for trigger in schema_module._PERMISSION_RECEIPT_TRIGGERS:
        connection.execute(f"DROP TRIGGER IF EXISTS {trigger}")
    connection.execute(f"DROP TABLE IF EXISTS {schema_module.permission_receipts.name}")


def _restore_v53_effect_operation_vocabulary(connection: sqlite3.Connection) -> None:
    """Take back the effect operation V54 added, keeping every stored effect.

    A store at V53 or earlier admits two effect operations where today's
    declaration admits three. Rebuilding both tables in the shape V53 published
    is the same rebuild the hop performs, run the other way -- the receipt
    first, so the child is rebuilt against the parent it keeps.
    """

    for table, parked, triggers in (
        (
            effect_receipts,
            "effect_receipts_after_claim_work_item",
            ("effect_receipts_no_update", "effect_receipts_no_delete"),
        ),
        (
            effect_intents,
            "effect_intents_after_claim_work_item",
            (
                "effect_intents_binding_no_update",
                "effect_intents_no_delete",
                "effect_intents_abandonment",
                "effect_intents_no_abandoned_insert",
            ),
        ),
    ):
        schema_module._rebuild_product_table(
            connection, table, parked, triggers, SCHEMA_VERSION, 53
        )


def _restore_v52_attempt_failure_vocabulary(connection: sqlite3.Connection) -> None:
    """Take back the failure code V53 added, keeping every stored attempt.

    A store at V52 or earlier admits eight attempt failure codes where today's
    declaration admits nine, in the table's own CHECK and in its transition
    trigger alike. Rebuilding it in the shape V52 published is the same rebuild
    the hop performs, run the other way. It goes back past the effect
    vocabulary V54 added as well, which a V52 store predates just as a V53 one
    does.
    """

    _restore_v53_effect_operation_vocabulary(connection)
    schema_module._rebuild_product_table(
        connection,
        agent_attempts,
        "agent_attempts_after_produced_value_refused",
        schema_module._AGENT_ATTEMPTS_TRIGGERS,
        53,
        52,
        trigger_source=schema_module._V52_AGENT_ATTEMPT_TRIGGERS,
    )


def _restore_v49_attempt_failure_vocabulary(connection: sqlite3.Connection) -> None:
    """Take back the failure code V50 added, keeping every stored attempt.

    A store at V49 or earlier admits seven attempt failure codes where today's
    declaration admits eight, in the table's own CHECK and in its transition
    trigger alike. Rebuilding it in the shape V49 published is what makes such
    a store's fingerprint the one its declared version claims -- and it is the
    same rebuild the hop performs, run the other way. Past the ledger V51
    added, which a V49 store predates just as a V50 one does.
    """

    _restore_v50_permission_ledger_predecessor(connection)
    schema_module._rebuild_product_table(
        connection,
        agent_attempts,
        "agent_attempts_after_candidate_unchanged",
        schema_module._AGENT_ATTEMPTS_TRIGGERS,
        50,
        49,
        trigger_source=schema_module._V49_AGENT_ATTEMPT_TRIGGERS,
    )


def _restore_v48_definition_source_predecessor(
    connection: sqlite3.Connection,
) -> None:
    """Take back the three tables V49 added, leaving every other table alone.

    V49 is additive, so a predecessor of it differs from today only by not
    carrying these; dropping them is the whole restoration -- past the
    vocabulary V50 widened, which a V48 store predates just as a V49 one does.
    """

    _restore_v49_attempt_failure_vocabulary(connection)
    for trigger in schema_module._DEFINITION_SOURCE_TRIGGERS:
        connection.execute(f"DROP TRIGGER IF EXISTS {trigger}")
    for table in reversed(schema_module._DEFINITION_SOURCE_TABLES):
        connection.execute(f"DROP TABLE IF EXISTS {table.name}")


def _restore_v45_answer_attribution_predecessors(
    connection: sqlite3.Connection,
) -> None:
    """Restore the exact event and answer tables published through V45."""

    _restore_v48_definition_source_predecessor(connection)
    for trigger in ("catalog_intakes_no_update", "catalog_intakes_no_delete"):
        connection.execute(f"DROP TRIGGER IF EXISTS {trigger}")
    connection.execute("DROP TABLE IF EXISTS catalog_intakes")

    # V48 gives queue_items its title-observation and retirement columns; no
    # hop before it moved this table since V44, so every restore below V48
    # needs it back at the byte-identical V44/V47 shape it published.
    schema_module._rebuild_product_table(
        connection,
        schema_module.queue_items,
        "queue_items_v48",
        (
            "queue_items_identity_no_update",
            "queue_items_no_delete",
            "queue_items_no_nonobserved_insert",
            "queue_items_state_transition",
        ),
        48,
        47,
        trigger_source={
            **schema_module._PRODUCT_TRIGGERS,
            "queue_items_state_transition": (
                PUBLISHED_QUEUE_ITEMS_STATE_TRANSITION_TRIGGER_BEFORE_OBSERVATION
            ),
        },
    )

    schema_module._rebuild_product_table(
        connection,
        schema_module.run_events,
        "run_events_v46",
        schema_module._RUN_EVENTS_TRIGGERS,
        46,
        45,
    )

    schema_module._rebuild_product_table(
        connection,
        schema_module.wait_answers,
        "wait_answers_v46",
        schema_module._WAIT_ANSWERS_TRIGGERS,
        46,
        45,
        trigger_source=schema_module.PUBLISHED_WAIT_ANSWER_TRIGGERS[45],
    )


def _restore_v39_configuration_tables(connection: sqlite3.Connection) -> None:
    """Turn the current create path back into V39's retired configuration shape."""

    _restore_v40_fork_predecessor(connection)
    for trigger in (
        "host_project_model_defaults_no_delete",
        "host_project_model_defaults_no_update",
        "host_project_model_defaults_revisions_no_delete",
        "host_project_model_defaults_revisions_no_update",
        "host_model_registry_entries_no_delete",
        "host_model_registry_entries_no_update",
        "host_model_registry_revisions_no_delete",
        "host_model_registry_revisions_no_update",
    ):
        connection.execute(f"DROP TRIGGER {trigger}")
    for table in (
        host_project_model_defaults,
        host_project_model_defaults_revisions,
        host_model_registry_entries,
        host_model_registry_revisions,
    ):
        connection.execute(f"DROP TABLE {table.name}")
    connection.execute(PUBLISHED_TABLE_SHAPES[(39, "host_occupancy_revisions")])
    connection.execute(PUBLISHED_TABLE_SHAPES[(39, "host_occupancy_bindings")])
    for statement in _OCCUPANCY_TRIGGER_STATEMENTS.values():
        connection.execute(statement)


def _restore_v40_fork_predecessor(connection: sqlite3.Connection) -> None:
    """Remove V41's additive fork family and restore V40's receipt shape."""

    _restore_v41_operation_predecessor(connection)

    for trigger in (
        "run_fork_effect_fences_no_delete",
        "run_fork_effect_fences_no_update",
        "run_fork_reused_nodes_no_delete",
        "run_fork_reused_nodes_no_update",
        "run_forks_no_delete",
        "run_forks_no_update",
        "effect_receipts_no_delete",
        "effect_receipts_no_update",
    ):
        connection.execute(f"DROP TRIGGER {trigger}")
    for table in (
        "run_fork_effect_fences",
        "run_fork_reused_nodes",
        "run_forks",
    ):
        connection.execute(f"DROP TABLE {table}")
    connection.execute("PRAGMA legacy_alter_table=ON")
    try:
        connection.execute("ALTER TABLE effect_receipts RENAME TO effect_receipts_v41")
    finally:
        connection.execute("PRAGMA legacy_alter_table=OFF")
    connection.execute(PUBLISHED_TABLE_SHAPES[(40, "effect_receipts")])
    columns = (
        "logical_key, run_id, canonical_request, request_hash, "
        "workflow_revision_hash, adapter_revision, destination_identity, "
        "adapter_operational_identity, effect_id, result, result_hash, "
        "confirmation_source, reconcile_command_id"
    )
    connection.execute(
        f"INSERT INTO effect_receipts ({columns}) "
        f"SELECT {columns} FROM effect_receipts_v41"
    )
    connection.execute("DROP TABLE effect_receipts_v41")
    for trigger in ("effect_receipts_no_update", "effect_receipts_no_delete"):
        connection.execute(_PRODUCT_TRIGGERS[trigger])


def _restore_v41_operation_predecessor(connection: sqlite3.Connection) -> None:
    """Restore the two V41 effect tables before operation identity was durable."""

    _restore_v43_queue_predecessor(connection)
    intent_triggers = (
        "effect_intents_binding_no_update",
        "effect_intents_no_delete",
        "effect_intents_abandonment",
        "effect_intents_no_abandoned_insert",
    )
    receipt_triggers = ("effect_receipts_no_update", "effect_receipts_no_delete")
    for trigger in (*intent_triggers, *receipt_triggers):
        connection.execute(f"DROP TRIGGER {trigger}")
    for table_name, parked, columns in (
        (
            "effect_receipts",
            "effect_receipts_v42",
            (
                "logical_key, run_id, canonical_request, request_hash, "
                "workflow_revision_hash, adapter_revision, destination_identity, "
                "adapter_operational_identity, effect_id, result, result_hash, "
                "confirmation_source, reconcile_command_id, fork_source_logical_key, "
                "fork_source_run_id, fork_source_workflow_revision_hash, "
                "fork_source_result_hash"
            ),
        ),
        (
            "effect_intents",
            "effect_intents_v42",
            (
                "logical_key, run_id, canonical_request, request_hash, "
                "workflow_revision_hash, adapter_revision, destination_identity, "
                "adapter_operational_identity, state, state_version, "
                "reconciliation_owner_command_id"
            ),
        ),
    ):
        connection.execute("PRAGMA legacy_alter_table=ON")
        try:
            connection.execute(f"ALTER TABLE {table_name} RENAME TO {parked}")
        finally:
            connection.execute("PRAGMA legacy_alter_table=OFF")
        connection.execute(PUBLISHED_TABLE_SHAPES[(41, table_name)])
        connection.execute(
            f"INSERT INTO {table_name} ({columns}) SELECT {columns} FROM {parked}"
        )
        connection.execute(f"DROP TABLE {parked}")
    for trigger in (*intent_triggers, *receipt_triggers):
        connection.execute(_V41_EFFECT_INTENT_TRIGGERS[trigger])


def _create_populated_v40_store(database_path: Path) -> tuple[object, ...]:
    """Build the exact predecessor with one confirmed receipt the rebuild must keep."""

    engine = create_canonical_engine(database_path)
    initialize_schema(engine)
    engine.dispose()
    revision_hash = "41" * 32
    request_hash = "42" * 32
    result_hash = "43" * 32
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "INSERT INTO workflow_revisions (revision_hash, document) VALUES (?, ?)",
            (revision_hash, b"name: migrated-effect\nsteps: []\n"),
        )
        connection.execute(
            "INSERT INTO runs (run_id, bootstrap_workflow_id, revision_hash, "
            "workflow_format_version, current_node_id, current_round_ordinal, "
            "state, state_version, last_event_sequence, terminal_hash) "
            "VALUES (?, ?, ?, 1, ?, 1, 'COMPLETED', 1, 0, ?)",
            (
                "v40-effect-run",
                "v40-effect-bootstrap",
                revision_hash,
                "effect",
                "44" * 32,
            ),
        )
        shared = (
            "v40-effect",
            "v40-effect-run",
            b"one canonical request",
            request_hash,
            revision_hash,
            "open-pr/v1",
            "repository/project",
            "github/project",
        )
        connection.execute(
            "INSERT INTO effect_intents (logical_key, run_id, canonical_request, "
            "request_hash, workflow_revision_hash, adapter_revision, "
            "destination_identity, adapter_operational_identity, operation_name, "
            "state, state_version) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open-pr', "
            "'CONFIRMED', 1)",
            shared,
        )
        connection.execute(
            "INSERT INTO effect_receipts (logical_key, run_id, canonical_request, "
            "request_hash, workflow_revision_hash, adapter_revision, "
            "destination_identity, adapter_operational_identity, operation_name, "
            "effect_id, result, result_hash, confirmation_source) VALUES (?, ?, ?, "
            "?, ?, ?, ?, ?, 'open-pr', ?, ?, ?, 'ADAPTER_EXECUTION')",
            (*shared, "pull-request/41", b"confirmed result", result_hash),
        )
        _restore_v40_fork_predecessor(connection)
        connection.execute("DROP TRIGGER agent_attempt_receipts_v3_no_update")
        connection.execute("DROP TRIGGER agent_attempt_receipts_v3_no_delete")
        connection.execute("DROP TABLE agent_attempt_receipts_v3")
        connection.execute(
            "UPDATE atelier_schema_versions SET version = ?",
            (V40_SCHEMA_HANDOFF.version,),
        )
        connection.commit()
        _require_product_shape(connection, V40_SCHEMA_HANDOFF.version)
        receipt = connection.execute(
            "SELECT * FROM effect_receipts WHERE logical_key = 'v40-effect'"
        ).fetchone()
    assert receipt is not None
    return receipt


def test_populated_v40_receipt_crosses_the_v41_hop_unchanged(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "atelier.sqlite"
    predecessor_receipt = _create_populated_v40_store(database_path)

    report = migrate_store(database_path)

    assert (report.source_version, report.target_version) == (
        V40_SCHEMA_HANDOFF.version,
        SCHEMA_VERSION,
    )
    with sqlite3.connect(database_path) as connection:
        migrated = connection.execute(
            "SELECT * FROM effect_receipts WHERE logical_key = 'v40-effect'"
        ).fetchone()
        assert migrated == (
            *predecessor_receipt[:8],
            "open-pr",
            *predecessor_receipt[8:],
            None,
            None,
            None,
            None,
        )
        assert connection.execute(
            "SELECT version FROM atelier_schema_versions"
        ).fetchone() == (SCHEMA_VERSION,)
        for table in (run_forks, run_fork_reused_nodes, run_fork_effect_fences):
            assert connection.execute(
                f"SELECT count(*) FROM {table.name}"
            ).fetchone() == (0,)


def test_v41_failpoint_after_version_cas_restores_the_exact_v40_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = tmp_path / "atelier.sqlite"
    _create_populated_v40_store(database_path)
    before = _logical_dump(database_path)
    raise_declared_version = schema_module._raise_declared_version

    def fail_after_v41_cas(
        connection: sqlite3.Connection, expected_version: int, target_version: int
    ) -> None:
        raise_declared_version(connection, expected_version, target_version)
        if expected_version == V40_SCHEMA_HANDOFF.version:
            raise sqlite3.OperationalError("v41-after-version-cas-failpoint")

    monkeypatch.setattr(schema_module, "_raise_declared_version", fail_after_v41_cas)

    with pytest.raises(StoreMigrationRefused, match="v41-after-version-cas-failpoint"):
        migrate_store(database_path)

    assert _logical_dump(database_path) == before
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT version FROM atelier_schema_versions"
        ).fetchone() == (V40_SCHEMA_HANDOFF.version,)
        _require_product_shape(connection, V40_SCHEMA_HANDOFF.version)


def _create_populated_v41_store(
    database_path: Path, *, corrupt_receipt_binding: bool = False
) -> None:
    engine = create_canonical_engine(database_path)
    initialize_schema(engine)
    engine.dispose()
    revision = "51" * 32
    request_hash = "52" * 32
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "INSERT INTO workflow_revisions VALUES (?, ?)",
            (revision, b"name: v41\nsteps: []\n"),
        )
        connection.execute(
            "INSERT INTO runs (run_id, bootstrap_workflow_id, revision_hash, "
            "workflow_format_version, current_node_id, current_round_ordinal, "
            "state, state_version, last_event_sequence) VALUES "
            "('v41-run', 'v41-bootstrap', ?, 1, 'effect', 1, 'STARTED', 1, 0)",
            (revision,),
        )
        shared = (
            "v41-effect",
            "v41-run",
            b"request",
            request_hash,
            revision,
            "adapter-v1",
            "github",
            "github:owner/repository",
        )
        connection.execute(
            "INSERT INTO effect_intents VALUES (?, ?, ?, ?, ?, ?, ?, ?, "
            "'open-pr', 'CONFIRMED', 1, NULL)",
            shared,
        )
        receipt_binding = (
            *shared[:6],
            "corrupt-destination" if corrupt_receipt_binding else shared[6],
            *shared[7:],
        )
        connection.execute(
            "INSERT INTO effect_receipts VALUES (?, ?, ?, ?, ?, ?, ?, ?, "
            "'open-pr', 'pr/1', X'01', ?, 'ADAPTER_EXECUTION', NULL, NULL, NULL, "
            "NULL, NULL)",
            (*receipt_binding, "53" * 32),
        )
        _restore_v41_operation_predecessor(connection)
        connection.execute("DROP TRIGGER agent_attempt_receipts_v3_no_update")
        connection.execute("DROP TRIGGER agent_attempt_receipts_v3_no_delete")
        connection.execute("DROP TABLE agent_attempt_receipts_v3")
        connection.execute(
            "UPDATE atelier_schema_versions SET version = ?",
            (V41_SCHEMA_HANDOFF.version,),
        )
        connection.commit()
        _require_product_shape(connection, V41_SCHEMA_HANDOFF.version)


def test_v41_effect_rows_cross_v42_with_open_pr_backfilled(tmp_path: Path) -> None:
    database = tmp_path / "atelier.sqlite"
    _create_populated_v41_store(database)

    report = migrate_store(database)

    assert (report.source_version, report.target_version) == (41, SCHEMA_VERSION)
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT operation_name FROM effect_intents"
        ).fetchone() == ("open-pr",)
        assert connection.execute(
            "SELECT operation_name FROM effect_receipts"
        ).fetchone() == ("open-pr",)


def test_v41_receipt_binding_corruption_is_refused_without_mutation(
    tmp_path: Path,
) -> None:
    database = tmp_path / "atelier.sqlite"
    _create_populated_v41_store(database, corrupt_receipt_binding=True)
    before = _logical_dump(database)

    with pytest.raises(StoreMigrationRefused, match="binding differs"):
        migrate_store(database)

    assert _logical_dump(database) == before


def _create_populated_v42_store(
    database_path: Path, *, colliding_receipt_table: bool = False
) -> None:
    engine = create_canonical_engine(database_path)
    initialize_schema(engine)
    engine.dispose()
    artifact_content = b"v42 row preserved byte-for-byte"
    with sqlite3.connect(database_path) as connection:
        _restore_v43_queue_predecessor(connection)
        receipt_table_ddl = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' "
            "AND name='agent_attempt_receipts_v3'"
        ).fetchone()
        assert receipt_table_ddl is not None
        connection.execute(
            "INSERT INTO artifacts (artifact_hash, content) VALUES (?, ?)",
            (Sha256Hash.of(artifact_content).value, artifact_content),
        )
        connection.execute(
            "INSERT INTO workflow_revisions (revision_hash, document) VALUES (?, ?)",
            ("a2" * 32, b"name: populated-v42\nsteps: []\n"),
        )
        connection.execute("DROP TRIGGER agent_attempt_receipts_v3_no_update")
        connection.execute("DROP TRIGGER agent_attempt_receipts_v3_no_delete")
        connection.execute("DROP TABLE agent_attempt_receipts_v3")
        connection.execute(
            "UPDATE atelier_schema_versions SET version = ?",
            (V42_SCHEMA_HANDOFF.version,),
        )
        if colliding_receipt_table:
            connection.execute(str(receipt_table_ddl[0]))
            connection.execute(
                "INSERT INTO agent_attempt_receipts_v3 VALUES (?, ?, ?, ?, ?, ?)",
                (
                    "b2" * 32,
                    "collision",
                    "c2" * 32,
                    "d2" * 32,
                    None,
                    "e2" * 32,
                ),
            )
        connection.commit()
        if not colliding_receipt_table:
            _require_product_shape(connection, V42_SCHEMA_HANDOFF.version)


def _v42_product_rows(
    database_path: Path,
) -> tuple[tuple[str, tuple[object, ...]], ...]:
    with sqlite3.connect(database_path) as connection:
        return tuple(
            (
                table_name,
                tuple(
                    tuple(row)
                    for row in connection.execute(
                        f'SELECT * FROM "{table_name}" ORDER BY rowid'
                    )
                ),
            )
            for table_name in sorted(
                schema_module._table_names_for_version(V42_SCHEMA_HANDOFF.version)
                - {atelier_schema_versions.name}
            )
        )


def test_every_populated_v42_product_row_crosses_v43_and_v44_unchanged(
    tmp_path: Path,
) -> None:
    assert V42_SCHEMA_HANDOFF.fingerprint_sha256 == (
        "d2f874edd0dbbecb677b284db8e41cd3a681fae99703d126764bc90fa0cf7865"
    )
    assert V43_SCHEMA_HANDOFF.fingerprint_sha256 == (
        "f7d299ab865b87ca47a399d4897f8c7b273085c4d206fac9eb882d47198b9782"
    )
    assert V44_SCHEMA_HANDOFF.fingerprint_sha256 == (
        "b8a176e76092a24fa0c8ac1caafdd69e57f4ff404ecb5560a1dd426d32a3ee9b"
    )
    database = tmp_path / "atelier.sqlite"
    _create_populated_v42_store(database)
    before = _v42_product_rows(database)

    report = migrate_store(database)

    assert (report.source_version, report.target_version) == (42, SCHEMA_VERSION)
    assert _v42_product_rows(database) == before
    with sqlite3.connect(database) as connection:
        assert schema_module._fingerprint_for_version(connection, SCHEMA_VERSION) == (
            PRODUCT_SCHEMA_HANDOFF.fingerprint_sha256
        )


def test_nonempty_v43_receipt_table_collision_refuses_v42_without_mutation(
    tmp_path: Path,
) -> None:
    database = tmp_path / "atelier.sqlite"
    _create_populated_v42_store(database, colliding_receipt_table=True)
    before = _logical_dump(database)

    with pytest.raises(
        StoreMigrationRefused,
        match="schema version 42 already has agent_attempt_receipts_v3",
    ):
        migrate_store(database)

    assert _logical_dump(database) == before


def test_v43_failpoint_after_version_cas_restores_the_exact_v42_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "atelier.sqlite"
    _create_populated_v42_store(database)
    before = _logical_dump(database)
    raise_declared_version = schema_module._raise_declared_version

    def fail_after_v43_cas(
        connection: sqlite3.Connection, expected_version: int, target_version: int
    ) -> None:
        raise_declared_version(connection, expected_version, target_version)
        if expected_version == V42_SCHEMA_HANDOFF.version:
            raise sqlite3.OperationalError("v43-after-version-cas-failpoint")

    monkeypatch.setattr(schema_module, "_raise_declared_version", fail_after_v43_cas)

    with pytest.raises(StoreMigrationRefused, match="v43-after-version-cas-failpoint"):
        migrate_store(database)

    assert _logical_dump(database) == before
    with sqlite3.connect(database) as connection:
        assert schema_module._fingerprint_for_version(connection, 42) == (
            V42_SCHEMA_HANDOFF.fingerprint_sha256
        )


def test_v44_process_loss_after_queue_copy_rolls_back_then_reruns_cleanly(
    tmp_path: Path,
) -> None:
    database = tmp_path / "atelier.sqlite"
    engine = create_canonical_engine(database)
    initialize_schema(engine)
    engine.dispose()
    with sqlite3.connect(database) as connection:
        _restore_v43_queue_predecessor(connection)
        connection.execute("UPDATE atelier_schema_versions SET version = 43")
        connection.commit()
        _require_product_shape(connection, 43)
    before = _logical_dump(database)
    child = get_context("spawn").Process(
        target=_crash_v44_migration_after_queue_copy,
        args=(str(database),),
    )
    child.start()
    child.join(timeout=20)
    if child.is_alive():
        child.terminate()
        child.join(timeout=5)
        if child.is_alive():
            child.kill()
            child.join(timeout=5)
        pytest.fail("the crashing migration child did not exit within 20 seconds")

    assert child.exitcode == 79
    assert _logical_dump(database) == before
    with sqlite3.connect(database) as connection:
        assert schema_module._fingerprint_for_version(connection, 43) == (
            V43_SCHEMA_HANDOFF.fingerprint_sha256
        )

    report = migrate_store(database)

    assert (report.source_version, report.target_version) == (43, SCHEMA_VERSION)
    with sqlite3.connect(database) as connection:
        assert schema_module._fingerprint_for_version(connection, SCHEMA_VERSION) == (
            PRODUCT_SCHEMA_HANDOFF.fingerprint_sha256
        )


def _create_populated_v44_source_store(database: Path) -> None:
    engine = create_canonical_engine(database)
    initialize_schema(engine)
    engine.dispose()
    with sqlite3.connect(database) as connection:
        _restore_v44_connection_predecessor(connection)
        connection.execute("UPDATE atelier_schema_versions SET version = 44")
        predecessor_rows = (
            (1, "acme/studio@main", Path("/legacy/credential-one")),
            (2, "acme/studio@trunk", Path("/legacy/credential-two")),
        )
        connection.executemany(
            "INSERT INTO host_project_source_connection_revisions "
            "(revision_hash, project_id, source_kind, revision_number, "
            "source_address, credential_directory, auth_method, connected_by) "
            "VALUES (?, 'studio', 'github', ?, ?, ?, 'personal-access-token', "
            "'operator')",
            tuple(
                (
                    schema_module._v44_project_source_connection_revision_hash(
                        ProjectId("studio"),
                        revision_number,
                        SourceKind("github"),
                        SourceAddress(source_address),
                        credential_directory,
                        SourceConnectionAuthMethod.PERSONAL_ACCESS_TOKEN,
                        ConnectionActor("operator"),
                    ),
                    revision_number,
                    source_address,
                    str(credential_directory),
                )
                for revision_number, source_address, credential_directory in predecessor_rows
            ),
        )
        connection.execute(
            "INSERT INTO queue_items "
            "(item_id, project_id, tracker_item_reference, state, state_version) "
            "VALUES (?, 'studio', 'gh:567', 'OBSERVED', 0)",
            ("c" * 64,),
        )
        connection.commit()
        _require_product_shape(connection, 44)


_QueueSchemaObject = tuple[object, ...]
_QueueTableRows = tuple[tuple[object, ...], ...]
_QueueSnapshot = tuple[
    tuple[_QueueSchemaObject, ...], tuple[tuple[str, _QueueTableRows], ...]
]


def _queue_schema_and_rows(connection: sqlite3.Connection) -> _QueueSnapshot:
    objects = tuple(
        connection.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master "
            "WHERE name LIKE 'queue_%' OR tbl_name LIKE 'queue_%' "
            "ORDER BY type, name"
        )
    )
    table_names = tuple(str(record[1]) for record in objects if record[0] == "table")
    rows = tuple(
        (table_name, tuple(connection.execute(f"SELECT * FROM {table_name}")))
        for table_name in table_names
    )
    return objects, rows


def _queue_table_names(snapshot: _QueueSnapshot) -> frozenset[str]:
    objects, _rows = snapshot
    return frozenset(str(record[1]) for record in objects if record[0] == "table")


@pytest.mark.proves("v44-project-source-history-migrates-as-one-replayable-hop")
def test_v45_preserves_legacy_source_history_without_touching_queue_objects(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The V44->V45 source hop invents no queue decision.

    `main(["migrate", ...])` raises the store all the way to today's schema,
    not just to V45, so a later hop's own legitimate change to queue_items
    (V48's title-observation and retirement columns) is expected to appear in
    the after snapshot. What this test proves is narrower and still holds:
    no queue table was added or removed, and the one stored decision keeps
    every value it already had, with the new columns unwritten.
    """

    database = tmp_path / "atelier.sqlite"
    _create_populated_v44_source_store(database)
    with sqlite3.connect(database) as connection:
        queue_before = _queue_schema_and_rows(connection)
        queue_table_names_before = _queue_table_names(queue_before)

    assert main(["migrate", "--database", str(database)]) == 0

    assert "step 44 -> 45" in capsys.readouterr().out
    with sqlite3.connect(database) as connection:
        rows = tuple(
            connection.execute(
                "SELECT revision_hash, source_id, revision_number, source_address, "
                "source_ref, credential_directory, lifecycle, connected_at "
                "FROM host_project_source_connection_revisions "
                "ORDER BY revision_number"
            )
        )
        assert tuple(row[1:] for row in rows) == (
            (
                "305615fd-f3be-c40f-22d4-7e8d53428878",
                1,
                "acme/studio",
                "main",
                "/legacy/credential-one",
                "DISCONNECTED",
                None,
            ),
            (
                "305615fd-f3be-c40f-22d4-7e8d53428878",
                2,
                "acme/studio",
                "trunk",
                "/legacy/credential-two",
                "CONNECTED",
                None,
            ),
        )
        assert tuple(row[0] for row in rows) != ("a" * 64, "b" * 64)
        assert all(
            len(str(row[0])) == 64
            and not set(str(row[0])).difference("0123456789abcdef")
            for row in rows
        )
        queue_after = _queue_schema_and_rows(connection)
        assert _queue_table_names(queue_after) == queue_table_names_before
        before_rows = dict(queue_before[1])
        after_rows = dict(queue_after[1])
        for table_name, before_table_rows in before_rows.items():
            after_table_rows = after_rows[table_name]
            if table_name == "queue_items":
                # V44 through V47 published nine columns; V48 appends the
                # three observation/retirement columns this hop adds.
                predecessor_column_count = 9
                assert (
                    tuple(row[:predecessor_column_count] for row in after_table_rows)
                    == before_table_rows
                )
                assert all(
                    row[predecessor_column_count:] == (None, None, None)
                    for row in after_table_rows
                )
            else:
                assert after_table_rows == before_table_rows
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                "UPDATE host_project_source_connection_revisions "
                "SET source_address = 'changed'"
            )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute("DELETE FROM host_project_source_connection_revisions")
    engine = create_canonical_engine(database)
    try:
        with engine.connect() as connection:
            records = connection.execute(
                sa.select(host_project_source_connection_revisions).order_by(
                    host_project_source_connection_revisions.c.revision_number
                )
            ).mappings()
            assert (
                len(
                    tuple(
                        project_source_connection_revision_from_record(row)
                        for row in records
                    )
                )
                == 2
            )
    finally:
        engine.dispose()

    before_replay = _logical_dump(database)
    assert main(["migrate", "--database", str(database)]) == 0
    assert "already current" in capsys.readouterr().out
    assert _logical_dump(database) == before_replay


@pytest.mark.proves("v44-project-source-history-migrates-as-one-replayable-hop")
def test_v44_connection_shape_is_frozen_apart_from_the_live_v45_declaration() -> None:
    frozen = PUBLISHED_TABLE_SHAPES[(44, host_project_source_connection_revisions.name)]

    assert (
        schema_module._table_shape_at(44, host_project_source_connection_revisions)
        == frozen
    )
    assert "source_id" not in frozen
    assert "source_ref" not in frozen
    assert "source_ref" in schema_module._table_shape_at(
        SCHEMA_VERSION, host_project_source_connection_revisions
    )


@pytest.mark.proves("v44-project-source-history-migrates-as-one-replayable-hop")
def test_v45_marks_every_historical_revision_disconnected_across_source_kinds(
    tmp_path: Path,
) -> None:
    database = tmp_path / "atelier.sqlite"
    _create_populated_v44_source_store(database)
    with sqlite3.connect(database) as connection:
        legacy_gitlab_revisions = (
            (1, "opaque:project@first-ref", "~/opaque/../credential-one"),
            (3, "opaque:project@current-ref", "~/opaque/../credential-three"),
        )
        connection.executemany(
            "INSERT INTO host_project_source_connection_revisions "
            "(revision_hash, project_id, source_kind, revision_number, "
            "source_address, credential_directory, auth_method, connected_by) "
            "VALUES (?, 'studio', 'gitlab', ?, ?, ?, "
            "'personal-access-token', 'legacy-operator')",
            tuple(
                (
                    schema_module._v44_project_source_connection_revision_hash(
                        ProjectId("studio"),
                        revision_number,
                        SourceKind("gitlab"),
                        SourceAddress(source_address),
                        credential_reference,
                        SourceConnectionAuthMethod.PERSONAL_ACCESS_TOKEN,
                        ConnectionActor("legacy-operator"),
                    ),
                    revision_number,
                    source_address,
                    credential_reference,
                )
                for revision_number, source_address, credential_reference in legacy_gitlab_revisions
            ),
        )
        connection.commit()

    migrate_store(database)

    with sqlite3.connect(database) as connection:
        rows = tuple(
            connection.execute(
                "SELECT source_id, source_kind, revision_number, source_address, "
                "source_ref, credential_directory, lifecycle, connected_at "
                "FROM host_project_source_connection_revisions "
                "ORDER BY source_kind, revision_number"
            )
        )
    assert rows == (
        (
            "305615fd-f3be-c40f-22d4-7e8d53428878",
            "github",
            1,
            "acme/studio",
            "main",
            "/legacy/credential-one",
            "DISCONNECTED",
            None,
        ),
        (
            "305615fd-f3be-c40f-22d4-7e8d53428878",
            "github",
            2,
            "acme/studio",
            "trunk",
            "/legacy/credential-two",
            "DISCONNECTED",
            None,
        ),
        (
            "75c2fcf6-d0f3-ae58-9936-c93792d856ea",
            "gitlab",
            1,
            "opaque:project@first-ref",
            None,
            "~/opaque/../credential-one",
            "DISCONNECTED",
            None,
        ),
        (
            "75c2fcf6-d0f3-ae58-9936-c93792d856ea",
            "gitlab",
            3,
            "opaque:project@current-ref",
            None,
            "~/opaque/../credential-three",
            "CONNECTED",
            None,
        ),
    )


@pytest.mark.proves("v44-project-source-history-migrates-as-one-replayable-hop")
def test_v45_refuses_a_project_with_an_ambiguous_current_kind_without_mutation(
    tmp_path: Path,
) -> None:
    database = tmp_path / "atelier.sqlite"
    _create_populated_v44_source_store(database)
    with sqlite3.connect(database) as connection:
        credential_reference = "/legacy/gitlab-credential"
        connection.execute(
            "INSERT INTO host_project_source_connection_revisions "
            "(revision_hash, project_id, source_kind, revision_number, "
            "source_address, credential_directory, auth_method, connected_by) "
            "VALUES (?, 'studio', 'gitlab', 2, 'group/project', ?, "
            "'personal-access-token', 'legacy-operator')",
            (
                schema_module._v44_project_source_connection_revision_hash(
                    ProjectId("studio"),
                    2,
                    SourceKind("gitlab"),
                    SourceAddress("group/project"),
                    credential_reference,
                    SourceConnectionAuthMethod.PERSONAL_ACCESS_TOKEN,
                    ConnectionActor("legacy-operator"),
                ),
                credential_reference,
            ),
        )
        connection.commit()
    before = _logical_dump(database)

    with pytest.raises(
        StoreMigrationRefused,
        match=(
            "durable project-source corruption: project 'studio' has 2 current "
            "kinds; expected exactly one"
        ),
    ):
        migrate_store(database)

    assert _logical_dump(database) == before


@pytest.mark.proves("v44-project-source-history-migrates-as-one-replayable-hop")
def test_v45_failure_after_version_cas_restores_exact_v44_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "atelier.sqlite"
    _create_populated_v44_source_store(database)
    before = _logical_dump(database)
    raise_declared_version = schema_module._raise_declared_version

    def fail_after_v45_cas(
        connection: sqlite3.Connection, expected_version: int, target_version: int
    ) -> None:
        raise_declared_version(connection, expected_version, target_version)
        if expected_version == V44_SCHEMA_HANDOFF.version:
            raise sqlite3.OperationalError("v45-after-version-cas-failpoint")

    monkeypatch.setattr(schema_module, "_raise_declared_version", fail_after_v45_cas)

    with pytest.raises(StoreMigrationRefused, match="v45-after-version-cas-failpoint"):
        migrate_store(database)

    assert _logical_dump(database) == before
    with sqlite3.connect(database) as connection:
        assert schema_module._fingerprint_for_version(connection, 44) == (
            V44_SCHEMA_HANDOFF.fingerprint_sha256
        )


def test_v45_refuses_a_corrupt_v44_connection_hash_without_mutation(
    tmp_path: Path,
) -> None:
    database = tmp_path / "atelier.sqlite"
    _create_populated_v44_source_store(database)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "DROP TRIGGER host_project_source_connection_revisions_no_update"
        )
        connection.execute(
            "UPDATE host_project_source_connection_revisions "
            "SET revision_hash = ? WHERE revision_number = 2",
            ("f" * 64,),
        )
        connection.execute(
            schema_module._PRODUCT_TRIGGERS[
                "host_project_source_connection_revisions_no_update"
            ]
        )
        connection.commit()
    before = _logical_dump(database)

    with pytest.raises(
        StoreMigrationRefused,
        match="schema version 44 project-source connection hash does not match",
    ):
        migrate_store(database)

    assert _logical_dump(database) == before


@pytest.mark.proves("v44-project-source-history-migrates-as-one-replayable-hop")
def test_v45_refuses_a_legacy_github_location_without_a_base_ref(
    tmp_path: Path,
) -> None:
    database = tmp_path / "atelier.sqlite"
    _create_populated_v44_source_store(database)
    empty_ref_location = SourceAddress("acme/studio@")
    credential_directory = "/legacy/credential-two"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "DROP TRIGGER host_project_source_connection_revisions_no_update"
        )
        connection.execute(
            "UPDATE host_project_source_connection_revisions "
            "SET source_address = ?, revision_hash = ? "
            "WHERE project_id = 'studio' AND source_kind = 'github' "
            "AND revision_number = 2",
            (
                empty_ref_location.value,
                schema_module._v44_project_source_connection_revision_hash(
                    ProjectId("studio"),
                    2,
                    SourceKind("github"),
                    empty_ref_location,
                    credential_directory,
                    SourceConnectionAuthMethod.PERSONAL_ACCESS_TOKEN,
                    ConnectionActor("operator"),
                ),
            ),
        )
        connection.execute(
            schema_module._PRODUCT_TRIGGERS[
                "host_project_source_connection_revisions_no_update"
            ]
        )
        connection.commit()
    before = _logical_dump(database)

    with pytest.raises(
        StoreMigrationRefused,
        match="malformed GitHub project-source location",
    ):
        migrate_store(database)

    assert _logical_dump(database) == before


def test_migrate_refuses_a_hand_corrupted_partial_v44_queue_row(
    tmp_path: Path,
) -> None:
    database = tmp_path / "atelier.sqlite"
    engine = create_canonical_engine(database)
    initialize_schema(engine)
    item_id = "b2" * 32
    with engine.begin() as connection:
        connection.execute(
            queue_items.insert().values(
                item_id=item_id,
                project_id="project1",
                tracker_item_reference="gh:partial",
                state="OBSERVED",
                state_version=0,
            )
        )
    engine.dispose()
    with sqlite3.connect(database) as connection:
        _restore_v44_connection_predecessor(connection)
        connection.execute("UPDATE atelier_schema_versions SET version = 44")
        connection.execute("DROP TRIGGER queue_items_state_transition")
        connection.execute("PRAGMA ignore_check_constraints=ON")
        connection.execute(
            "UPDATE queue_items SET state='ADMITTED', state_version=2, "
            "workflow_lineage_id=?, admission_rationale='approved', "
            "current_proposal_revision=1 WHERE item_id=?",
            ("a1" * 32, item_id),
        )
        connection.commit()

    with pytest.raises(StoreMigrationRefused, match="partial or inconsistent V44"):
        migrate_store(database)


def test_predecessor_rebuild_without_a_frozen_shape_fails_loud() -> None:
    with pytest.raises(StoreMigrationRefused, match="no published shape of runs"):
        schema_module._table_shape_at(43, runs)


def test_v43_refusal_receipts_are_immutable_after_the_additive_hop(
    tmp_path: Path,
) -> None:
    database = tmp_path / "atelier.sqlite"
    _create_populated_v42_store(database)
    migrate_store(database)
    receipt = ("b3" * 32, "reason", "c3" * 32, "d3" * 32, None, "e3" * 32)

    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO agent_attempt_receipts_v3 VALUES (?, ?, ?, ?, ?, ?)",
            receipt,
        )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute("UPDATE agent_attempt_receipts_v3 SET reason='changed'")
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute("DELETE FROM agent_attempt_receipts_v3")


_PREDECESSOR_RUN_EVENTS_INDEX_DDL = (
    (
        "CREATE UNIQUE INDEX run_events_attempt_kind_unique ON run_events "
        "(agent_attempt_id, event_kind) WHERE agent_attempt_id IS NOT NULL"
    ),
    (
        "CREATE UNIQUE INDEX run_events_legacy_execution_kind_unique ON run_events "
        "(node_execution_id, event_kind) WHERE agent_attempt_id IS NULL"
    ),
    (
        "CREATE UNIQUE INDEX run_events_legacy_kind_unique ON run_events "
        "(run_id, revision_hash, node_id, event_kind) WHERE agent_attempt_id IS NULL"
    ),
)
"""The three event keys the store behind this fixture's table text published.

The V36 hop re-scoped the once-per-node key to the round, so a fixture that
builds a store from before that hop states the key as it stood, exactly as it
states the predecessor table text next to it.
"""


def _restore_predecessor_run_events(connection: Connection) -> None:
    triggers = ("run_events_no_update", "run_events_no_delete")
    for trigger in triggers:
        connection.execute(sa.text(f"DROP TRIGGER {trigger}"))
    for index in sorted(run_events.indexes, key=lambda index: index.name or ""):
        connection.execute(sa.text(f"DROP INDEX {index.name}"))
    connection.execute(sa.text("DROP TABLE run_events"))
    connection.execute(sa.text(_PREDECESSOR_RUN_EVENTS_DDL))
    for index_statement in _PREDECESSOR_RUN_EVENTS_INDEX_DDL:
        connection.execute(sa.text(index_statement))
    for trigger in triggers:
        connection.execute(sa.text(_PRODUCT_TRIGGERS[trigger]))


_PREDECESSOR_RUNS_DDL = """
CREATE TABLE runs (
run_id TEXT NOT NULL, 
bootstrap_workflow_id TEXT NOT NULL, 
revision_hash TEXT NOT NULL, 
workflow_format_version INTEGER NOT NULL, 
agent_binding_set_hash TEXT, 
current_node_id TEXT NOT NULL, 
state TEXT NOT NULL, 
state_version INTEGER NOT NULL, 
last_event_sequence INTEGER NOT NULL, 
terminal_hash TEXT, 
run_configuration_revision_hash TEXT, 
PRIMARY KEY (run_id), 
UNIQUE (run_id, revision_hash), 
UNIQUE (run_id, revision_hash, agent_binding_set_hash), 
CHECK (length(run_id) > 0), 
CHECK (length(current_node_id) > 0), 
CHECK (workflow_format_version IN (1, 2, 3)), 
CHECK ((workflow_format_version = 1 AND agent_binding_set_hash IS NULL) OR (workflow_format_version = 2 AND agent_binding_set_hash IS NOT NULL AND length(agent_binding_set_hash) = 64 AND agent_binding_set_hash NOT GLOB '*[^0-9a-f]*') OR (workflow_format_version = 3 AND (agent_binding_set_hash IS NULL OR (length(agent_binding_set_hash) = 64 AND agent_binding_set_hash NOT GLOB '*[^0-9a-f]*')))), 
CHECK (state IN ('STARTED', 'WAITING_RECONCILIATION', 'WAITING_INPUT', 'COMPLETED')), 
CHECK (state_version >= 0), 
CHECK (last_event_sequence >= 0), 
CHECK ((state = 'COMPLETED' AND terminal_hash IS NOT NULL AND length(terminal_hash) = 64 AND terminal_hash NOT GLOB '*[^0-9a-f]*') OR (state <> 'COMPLETED' AND terminal_hash IS NULL)), 
CHECK ((workflow_format_version = 3 AND run_configuration_revision_hash IS NOT NULL AND length(run_configuration_revision_hash) = 64 AND run_configuration_revision_hash NOT GLOB '*[^0-9a-f]*') OR (workflow_format_version <> 3 AND run_configuration_revision_hash IS NULL)), 
UNIQUE (bootstrap_workflow_id), 
FOREIGN KEY(revision_hash) REFERENCES workflow_revisions (revision_hash), 
FOREIGN KEY(run_configuration_revision_hash) REFERENCES run_configuration_revisions (revision_hash)
)
"""


def _restore_predecessor_runs(connection: Connection) -> None:
    connection.execute(sa.text("PRAGMA foreign_keys=OFF"))
    connection.execute(sa.text("DROP TRIGGER runs_binding_no_update"))
    connection.execute(sa.text("DROP TABLE runs"))
    connection.execute(sa.text(_PREDECESSOR_RUNS_DDL))
    connection.execute(sa.text(_PRODUCT_TRIGGERS["runs_binding_no_update"]))
    connection.execute(sa.text("PRAGMA foreign_keys=ON"))


_PREDECESSOR_AGENT_CONFIGURATION_REVISIONS_DDL = """
CREATE TABLE agent_configuration_revisions (
revision_hash TEXT NOT NULL, 
model TEXT NOT NULL, 
auth_profile_revision_hash TEXT NOT NULL, 
executor_revision TEXT NOT NULL, 
revision_format_version INTEGER NOT NULL, 
requested_capability TEXT NOT NULL, 
PRIMARY KEY (revision_hash), 
UNIQUE (revision_hash, auth_profile_revision_hash, model, executor_revision), 
UNIQUE (revision_hash, auth_profile_revision_hash, model, executor_revision, revision_format_version, requested_capability), 
CHECK (length(revision_hash) = 64 AND revision_hash NOT GLOB '*[^0-9a-f]*'), 
CHECK (length(model) BETWEEN 1 AND 1024), 
CHECK (length(auth_profile_revision_hash) = 64 AND auth_profile_revision_hash NOT GLOB '*[^0-9a-f]*'), 
CHECK (length(executor_revision) BETWEEN 1 AND 1024), 
CHECK (revision_format_version IN (1, 2)), 
CHECK (requested_capability IN ('headless', 'interactive')), 
CHECK (revision_format_version = 2 OR requested_capability = 'headless'), 
FOREIGN KEY(auth_profile_revision_hash) REFERENCES auth_profile_revisions (revision_hash)
)
"""


def _restore_predecessor_agent_configuration_revisions(
    connection: Connection,
) -> None:
    triggers = (
        "agent_configuration_revisions_no_update",
        "agent_configuration_revisions_no_delete",
    )
    connection.execute(sa.text("PRAGMA foreign_keys=OFF"))
    for trigger in triggers:
        connection.execute(sa.text(f"DROP TRIGGER {trigger}"))
    connection.execute(sa.text("DROP TABLE agent_configuration_revisions"))
    connection.execute(sa.text(_PREDECESSOR_AGENT_CONFIGURATION_REVISIONS_DDL))
    for trigger in triggers:
        connection.execute(sa.text(_PRODUCT_TRIGGERS[trigger]))
    connection.execute(sa.text("PRAGMA foreign_keys=ON"))


def _restore_predecessor_agent_attempts(connection: Connection) -> None:
    triggers = ("agent_attempts_state_transition", "agent_attempts_no_delete")
    for trigger in triggers:
        connection.execute(sa.text(f"DROP TRIGGER {trigger}"))
    connection.execute(sa.text("DROP TABLE agent_attempts"))
    connection.execute(sa.text(_PREDECESSOR_AGENT_ATTEMPTS_DDL))
    connection.execute(sa.text(_PREDECESSOR_AGENT_ATTEMPTS_TRIGGER_DDL))
    connection.execute(sa.text(_PRODUCT_TRIGGERS["agent_attempts_no_delete"]))


_PREDECESSOR_NODE_EXECUTION_REQUESTS_DDL = """
CREATE TABLE node_execution_requests_v3 (
    request_hash TEXT NOT NULL,
    node_execution_id TEXT NOT NULL,
    run_configuration_revision_hash TEXT NOT NULL,
    context_package_hash TEXT NOT NULL,
    preimage BLOB NOT NULL,
    PRIMARY KEY (request_hash),
    UNIQUE (node_execution_id, request_hash),
    CHECK (length(request_hash) = 64 AND request_hash NOT GLOB '*[^0-9a-f]*'),
    CHECK (length(node_execution_id) = 64 AND node_execution_id NOT GLOB '*[^0-9a-f]*'),
    CHECK (length(context_package_hash) = 64 AND context_package_hash NOT GLOB '*[^0-9a-f]*'),
    FOREIGN KEY(context_package_hash) REFERENCES context_packages_v3 (package_hash),
    FOREIGN KEY(run_configuration_revision_hash) REFERENCES run_configuration_revisions (revision_hash)
)
"""


def _restore_predecessor_node_execution_requests(connection: Connection) -> None:
    triggers = (
        "node_execution_requests_v3_no_update",
        "node_execution_requests_v3_no_delete",
    )
    for trigger in triggers:
        connection.execute(sa.text(f"DROP TRIGGER {trigger}"))
    connection.execute(sa.text("DROP TABLE node_execution_requests_v3"))
    connection.execute(sa.text(_PREDECESSOR_NODE_EXECUTION_REQUESTS_DDL))
    for trigger in triggers:
        connection.execute(sa.text(_PRODUCT_TRIGGERS[trigger]))


_PREDECESSOR_AGENT_RECEIPTS_DDL = """
CREATE TABLE agent_receipts_v2 (
    node_execution_id TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    run_id TEXT NOT NULL,
    workflow_revision_hash TEXT NOT NULL,
    node_id TEXT NOT NULL,
    role TEXT NOT NULL,
    binding_set_hash TEXT NOT NULL,
    agent_configuration_revision_hash TEXT NOT NULL,
    auth_profile_revision_hash TEXT NOT NULL,
    profile_id TEXT NOT NULL,
    revision_number INTEGER NOT NULL,
    provider_id TEXT NOT NULL,
    auth_mode TEXT NOT NULL,
    model TEXT NOT NULL,
    executor_revision TEXT NOT NULL,
    executor_operational_identity TEXT NOT NULL,
    output_bytes BLOB NOT NULL,
    output_hash TEXT NOT NULL,
    receipt_hash TEXT NOT NULL,
    PRIMARY KEY (node_execution_id),
    UNIQUE (run_id, workflow_revision_hash, node_id),
    FOREIGN KEY(run_id, workflow_revision_hash, binding_set_hash, role, agent_configuration_revision_hash) REFERENCES run_agent_bindings (run_id, revision_hash, binding_set_hash, role, agent_configuration_revision_hash),
    FOREIGN KEY(agent_configuration_revision_hash, auth_profile_revision_hash, model, executor_revision) REFERENCES agent_configuration_revisions (revision_hash, auth_profile_revision_hash, model, executor_revision),
    FOREIGN KEY(auth_profile_revision_hash, profile_id, revision_number, provider_id, auth_mode) REFERENCES auth_profile_revisions (revision_hash, profile_id, revision_number, provider_id, auth_mode),
    CHECK (length(node_execution_id) = 64 AND node_execution_id NOT GLOB '*[^0-9a-f]*'),
    CHECK (length(request_hash) = 64 AND request_hash NOT GLOB '*[^0-9a-f]*'),
    CHECK (length(run_id) > 0),
    CHECK (length(workflow_revision_hash) = 64 AND workflow_revision_hash NOT GLOB '*[^0-9a-f]*'),
    CHECK (length(node_id) BETWEEN 1 AND 1024),
    CHECK (length(role) BETWEEN 1 AND 1024),
    CHECK (length(binding_set_hash) = 64 AND binding_set_hash NOT GLOB '*[^0-9a-f]*'),
    CHECK (length(agent_configuration_revision_hash) = 64 AND agent_configuration_revision_hash NOT GLOB '*[^0-9a-f]*'),
    CHECK (length(auth_profile_revision_hash) = 64 AND auth_profile_revision_hash NOT GLOB '*[^0-9a-f]*'),
    CHECK (length(profile_id) BETWEEN 1 AND 1024),
    CHECK (revision_number BETWEEN 1 AND 9223372036854775807),
    CHECK (length(provider_id) BETWEEN 1 AND 64),
    CHECK (provider_id GLOB '[a-z]*'),
    CHECK (provider_id NOT GLOB '*[^a-z0-9._-]*'),
    CHECK (auth_mode IN ('subscription', 'api_key')),
    CHECK (length(model) BETWEEN 1 AND 1024),
    CHECK (length(executor_revision) BETWEEN 1 AND 1024),
    CHECK (length(executor_operational_identity) BETWEEN 1 AND 1024),
    CHECK (typeof(output_bytes) = 'blob' AND length(output_bytes) <= 49152),
    CHECK (length(output_hash) = 64 AND output_hash NOT GLOB '*[^0-9a-f]*'),
    CHECK (length(receipt_hash) = 64 AND receipt_hash NOT GLOB '*[^0-9a-f]*'),
    UNIQUE (receipt_hash)
)
"""


def _restore_predecessor_agent_receipts(connection: Connection) -> None:
    triggers = ("agent_receipts_v2_no_update", "agent_receipts_v2_no_delete")
    for trigger in triggers:
        connection.execute(sa.text(f"DROP TRIGGER {trigger}"))
    connection.execute(sa.text("DROP TABLE agent_receipts_v2"))
    connection.execute(sa.text(_PREDECESSOR_AGENT_RECEIPTS_DDL))
    for trigger in triggers:
        connection.execute(sa.text(_PRODUCT_TRIGGERS[trigger]))


def _archived_completion(revision_hash: WorkflowRevisionHash) -> RunEvent:
    """The completion an old run really wrote: no attempt binding, no receipt."""
    run_id = RunId(ARCHIVED_RUN_ID)
    return RunEvent(
        run_id,
        revision_hash,
        1,
        ARCHIVED_NODE_ID,
        NodeExecutionId.for_node(run_id, revision_hash, ARCHIVED_NODE_ID),
        RunEventKind.AGENT_COMPLETED,
        ARCHIVED_OUTPUT,
    )


def _create_populated_v13_store(database_path: Path) -> None:
    """An exact V13 product store, not a version-row witness.

    A fresh store of the current schema with each later table and its triggers
    removed, and every table a later hop reshapes restored to the shape it had
    at V13, is the published V13 shape. That is the same method as the #240 Z2
    testimony
    (predecessor schema from before the V14 head), expressed through today's
    owner so the fixture cannot drift from the create path the hop will reopen.
    """

    engine = create_canonical_engine(database_path)
    initialize_schema(engine)
    with sqlite3.connect(database_path) as predecessor:
        _restore_v39_configuration_tables(predecessor)
        _restore_v27_access_store(predecessor)
        _drop_queue_items_table(predecessor)
        _drop_webhook_delivery_cursor_table(predecessor)
        _drop_project_source_connection_table(predecessor)
        _revert_wait_answers_execution_key(predecessor)
        _revert_the_abandoned_intent_state(predecessor)
    published = PublishedRevision(RevisionKind.WORKFLOW, b"name: lasagne\n")
    lineage = CatalogLineage(published.kind, published.revision_hash)
    configuration = "44" * 32
    package = "33" * 32
    request = "22" * 32
    execution = "11" * 32
    receipt = "ef" * 32
    auth_profile_revision_hash = "55" * 32
    agent_configuration_revision_hash = ARCHIVED_AGENT_CONFIGURATION_HASH
    binding_set_hash = "77" * 32
    agent_receipt_hash = "dd" * 32
    with engine.connect() as connection:
        for table in (
            artifacts.name,
            run_inputs_v3.name,
            tool_redemptions.name,
            host_occupancy_bindings.name,
            host_occupancy_revisions.name,
            host_project_root_revisions.name,
        ):
            connection.execute(sa.text(f"DROP TRIGGER {table}_no_update"))
            connection.execute(sa.text(f"DROP TRIGGER {table}_no_delete"))
            connection.execute(sa.text(f"DROP TABLE {table}"))
        for trigger in (
            "run_instants_start_no_update",
            "run_instants_end_once",
            "run_instants_no_delete",
            "attempt_instants_start_no_update",
            "attempt_instants_end_once",
            "attempt_instants_no_delete",
            "event_instants_no_update",
            "event_instants_no_delete",
        ):
            connection.execute(sa.text(f"DROP TRIGGER {trigger}"))
        for table in (run_instants.name, attempt_instants.name, event_instants.name):
            connection.execute(sa.text(f"DROP TABLE {table}"))
        _restore_predecessor_run_events(connection)
        _restore_predecessor_agent_receipts(connection)
        _restore_predecessor_agent_attempts(connection)
        _restore_predecessor_runs(connection)
        _restore_predecessor_node_execution_requests(connection)
        _restore_predecessor_agent_configuration_revisions(connection)
        connection.execute(
            atelier_schema_versions.update()
            .where(atelier_schema_versions.c.version == SCHEMA_VERSION)
            .values(version=V13_SCHEMA_HANDOFF.version)
        )
        connection.execute(
            published_revisions.insert().values(
                kind=published.kind.value,
                revision_hash=published.revision_hash.value,
                document=published.document,
            )
        )
        connection.execute(
            catalog_lineages.insert().values(
                lineage_id=lineage.lineage_id.value,
                kind=published.kind.value,
                founding_revision_hash=published.revision_hash.value,
            )
        )
        connection.execute(
            catalog_lineage_members.insert().values(
                lineage_id=lineage.lineage_id.value,
                revision_number=1,
                revision_hash=published.revision_hash.value,
            )
        )
        connection.execute(
            workflow_revisions.insert().values(
                revision_hash=published.revision_hash.value,
                document=published.document,
            )
        )
        connection.execute(
            run_configuration_revisions.insert().values(
                revision_hash=configuration, preimage=b"one frozen resolution matrix"
            )
        )
        connection.execute(
            context_packages_v3.insert().values(
                package_hash=package, manifest=b"one supervised manifest"
            )
        )
        connection.execute(
            node_execution_requests_v3.insert().values(
                request_hash=request,
                node_execution_id=execution,
                run_configuration_revision_hash=configuration,
                context_package_hash=package,
                preimage=b"one node execution request",
            )
        )
        connection.execute(
            runs.insert().values(
                run_id=ARCHIVED_RUN_ID,
                bootstrap_workflow_id="bootstrap-archived-night-run",
                revision_hash=published.revision_hash.value,
                workflow_format_version=3,
                agent_binding_set_hash=binding_set_hash,
                current_node_id="cook",
                state="STARTED",
                state_version=1,
                last_event_sequence=1,
                terminal_hash=None,
                run_configuration_revision_hash=configuration,
            )
        )
        connection.execute(
            auth_profile_revisions.insert().values(
                revision_hash=auth_profile_revision_hash,
                profile_id="profile/archived",
                revision_number=1,
                provider_id="anthropic",
                auth_mode="api_key",
            )
        )
        connection.execute(
            agent_configuration_revisions.insert().values(
                revision_hash=agent_configuration_revision_hash,
                model=ARCHIVED_AGENT_MODEL,
                auth_profile_revision_hash=auth_profile_revision_hash,
                executor_revision="archived-executor",
                revision_format_version=1,
                requested_capability="headless",
            )
        )
        connection.execute(
            run_agent_bindings.insert().values(
                run_id=ARCHIVED_RUN_ID,
                revision_hash=published.revision_hash.value,
                binding_set_hash=binding_set_hash,
                role="chef",
                agent_configuration_revision_hash=agent_configuration_revision_hash,
            )
        )
        connection.execute(
            agent_attempts.insert().values(
                attempt_id=ARCHIVED_ATTEMPT_ID,
                node_execution_id=execution,
                request_hash=request,
                executor_operational_identity="operational/archived",
                run_id=ARCHIVED_RUN_ID,
                workflow_revision_hash=published.revision_hash.value,
                node_id="cook",
                attempt_ordinal=1,
                state="FAILED",
                state_version=2,
                process_phase="PROCESS_OBSERVED",
                process_owner_id="owner/archived",
                watchdog_generation_id="generation/archived",
                failure_code=ARCHIVED_ATTEMPT_FAILURE_CODE,
            )
        )
        archived = _archived_completion(
            WorkflowRevisionHash(published.revision_hash.value)
        )
        connection.execute(
            sa.text(
                "INSERT INTO run_events (run_id, revision_hash, event_sequence, "
                "node_id, node_execution_id, event_kind, payload, payload_hash, "
                "event_hash) VALUES (:run_id, :revision_hash, :event_sequence, "
                ":node_id, :node_execution_id, :event_kind, :payload, "
                ":payload_hash, :event_hash)"
            ),
            {
                "run_id": archived.run_id.value,
                "revision_hash": archived.revision_hash.value,
                "event_sequence": archived.event_sequence,
                "node_id": archived.node_id,
                "node_execution_id": archived.node_execution_id.value,
                "event_kind": archived.event_kind.value,
                "payload": archived.payload,
                "payload_hash": archived.payload_hash.value,
                "event_hash": archived.event_hash.value,
            },
        )
        connection.execute(
            node_receipts_v3.insert().values(
                node_execution_id=execution,
                disposition="succeeded",
                reason="completed",
                request_hash=request,
                context_package_hash=package,
                receipt_hash=receipt,
            )
        )
        connection.execute(
            sa.text(
                "INSERT INTO agent_receipts_v2 (node_execution_id, request_hash, "
                "run_id, workflow_revision_hash, node_id, role, binding_set_hash, "
                "agent_configuration_revision_hash, auth_profile_revision_hash, "
                "profile_id, revision_number, provider_id, auth_mode, model, "
                "executor_revision, executor_operational_identity, output_bytes, "
                "output_hash, receipt_hash) VALUES (:node_execution_id, "
                ":request_hash, :run_id, :workflow_revision_hash, :node_id, "
                ":role, :binding_set_hash, :agent_configuration_revision_hash, "
                ":auth_profile_revision_hash, :profile_id, :revision_number, "
                ":provider_id, :auth_mode, :model, :executor_revision, "
                ":executor_operational_identity, :output_bytes, :output_hash, "
                ":receipt_hash)"
            ),
            {
                "node_execution_id": ARCHIVED_RECEIPT_NODE_EXECUTION_ID,
                "request_hash": request,
                "run_id": ARCHIVED_RUN_ID,
                "workflow_revision_hash": published.revision_hash.value,
                "node_id": ARCHIVED_NODE_ID,
                "role": "chef",
                "binding_set_hash": binding_set_hash,
                "agent_configuration_revision_hash": agent_configuration_revision_hash,
                "auth_profile_revision_hash": auth_profile_revision_hash,
                "profile_id": "profile/archived",
                "revision_number": 1,
                "provider_id": "anthropic",
                "auth_mode": "api_key",
                "model": "archived-model",
                "executor_revision": "archived-executor",
                "executor_operational_identity": "operational/archived",
                "output_bytes": ARCHIVED_OUTPUT,
                "output_hash": "aa" * 32,
                "receipt_hash": agent_receipt_hash,
            },
        )
        connection.commit()
    engine.dispose()
    with sqlite3.connect(database_path) as connection:
        _require_product_shape(connection, V13_SCHEMA_HANDOFF.version)


@pytest.mark.proves("an-exact-v13-store-migrates-and-opens-as-the-current-schema")
def test_an_exact_v13_store_migrates_and_opens_as_the_current_schema(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database_path = tmp_path / "atelier.sqlite"
    _create_populated_v13_store(database_path)
    engine = create_canonical_engine(database_path)
    with pytest.raises(MigrationRequired, match="schema version 13"):
        initialize_schema(engine)
    engine.dispose()

    assert main(["migrate", "--database", str(database_path)]) == 0

    shown = capsys.readouterr()
    assert all(str(step) in shown.out for step in range(13, SCHEMA_VERSION + 1))
    assert PRODUCT_SCHEMA_HANDOFF.fingerprint_sha256 in shown.out

    engine = create_canonical_engine(database_path)
    initialize_schema(engine)
    with engine.connect() as connection:
        assert (
            connection.scalar(sa.select(atelier_schema_versions.c.version))
            == SCHEMA_VERSION
        )
        assert (
            connection.scalar(
                sa.select(runs.c.run_id).where(runs.c.run_id == ARCHIVED_RUN_ID)
            )
            == ARCHIVED_RUN_ID
        )
        assert (
            connection.scalar(
                sa.select(runs.c.current_round_ordinal).where(
                    runs.c.run_id == ARCHIVED_RUN_ID
                )
            )
            == FIRST_ROUND_ORDINAL
        )
        carried_receipt = (
            connection.execute(
                sa.select(agent_receipts_v2).where(
                    agent_receipts_v2.c.run_id == ARCHIVED_RUN_ID,
                    agent_receipts_v2.c.node_id == ARCHIVED_NODE_ID,
                )
            )
            .mappings()
            .one()
        )
        assert (
            carried_receipt["node_execution_id"] == ARCHIVED_RECEIPT_NODE_EXECUTION_ID
        )
        assert carried_receipt["round_ordinal"] == FIRST_ROUND_ORDINAL
        assert (
            connection.scalar(sa.select(node_receipts_v3.c.disposition)) == "succeeded"
        )
        attempt = (
            connection.execute(
                sa.select(agent_attempts).where(
                    agent_attempts.c.attempt_id == ARCHIVED_ATTEMPT_ID
                )
            )
            .mappings()
            .one()
        )
        assert attempt["state"] == "FAILED"
        assert attempt["failure_code"] == ARCHIVED_ATTEMPT_FAILURE_CODE
        assert attempt["runner_manifest_id"] is None
        assert attempt["runner_generation_id"] is None
        assert attempt["runner_invocation_id"] is None
        assert attempt["runner_terminal_evidence_hash"] is None
        assert attempt["runner_evidence_acceptance_phase"] == "NONE"
        assert (
            connection.scalar(sa.select(sa.func.count()).select_from(run_inputs_v3))
            == 0
        )
        assert (
            connection.scalar(sa.select(sa.func.count()).select_from(tool_redemptions))
            == 0
        )
        assert connection.scalar(sa.select(sa.func.count()).select_from(artifacts)) == 0
        assert (
            connection.scalar(
                sa.select(sa.func.count()).select_from(host_project_root_revisions)
            )
            == 0
        )
        archived = (
            connection.execute(
                sa.select(run_events).where(run_events.c.run_id == ARCHIVED_RUN_ID)
            )
            .mappings()
            .one()
        )
        expected = _archived_completion(
            WorkflowRevisionHash(str(archived["revision_hash"]))
        )
        assert bytes(archived["payload"]) == ARCHIVED_OUTPUT
        assert str(archived["event_hash"]) == expected.event_hash.value
        assert archived["agent_receipt_hash"] is None
        assert event_from_record(archived) == expected
        configuration = (
            connection.execute(sa.select(agent_configuration_revisions))
            .mappings()
            .one()
        )
        assert configuration["revision_hash"] == ARCHIVED_AGENT_CONFIGURATION_HASH
        assert configuration["model"] == ARCHIVED_AGENT_MODEL
        assert configuration["requested_capability"] == (
            AgentExecutionCapability.HEADLESS.value
        )
    # The widened vocabulary really arrived: the migrated store now publishes a
    # configuration the predecessor's CHECK would have refused.
    with engine.begin() as connection:
        connection.execute(
            agent_configuration_revisions.insert().values(
                revision_hash="cd" * 32,
                model=ARCHIVED_AGENT_MODEL,
                auth_profile_revision_hash=str(
                    configuration["auth_profile_revision_hash"]
                ),
                executor_revision="claude-subscription-tools/v1",
                revision_format_version=(
                    AgentConfigurationRevisionFormatVersion.V2.value
                ),
                requested_capability=(
                    AgentExecutionCapability.HEADLESS_WITH_TOOLS.value
                ),
            )
        )
    engine.dispose()


def _revert_project_verification_failed_attempts(
    connection: sqlite3.Connection,
) -> None:
    """Restore the three-code CHECK the PROJECT_VERIFICATION_FAILED hop left."""

    _revert_the_redemption_owner(connection)

    _rebuild_product_table(
        connection,
        agent_attempts,
        "agent_attempts_after_project_verification_failed",
        _AGENT_ATTEMPTS_TRIGGERS,
        SCHEMA_VERSION,
        V23_SCHEMA_HANDOFF.version,
        trigger_source=_V23_AGENT_ATTEMPT_TRIGGERS,
    )


def _revert_agent_refused_attempts(connection: sqlite3.Connection) -> None:
    """Restore the two-code CHECK the AGENT_REFUSED hop left behind."""

    _revert_the_redemption_owner(connection)

    _rebuild_product_table(
        connection,
        agent_attempts,
        "agent_attempts_after_agent_refused",
        _AGENT_ATTEMPTS_TRIGGERS,
        SCHEMA_VERSION,
        V22_SCHEMA_HANDOFF.version,
        trigger_source=_V17_AGENT_ATTEMPT_TRIGGERS,
    )


def _drop_host_project_root_channel(connection: sqlite3.Connection) -> None:
    connection.execute("DROP TRIGGER host_project_root_revisions_no_update")
    connection.execute("DROP TRIGGER host_project_root_revisions_no_delete")
    connection.execute(f"DROP TABLE {host_project_root_revisions.name}")


def _drop_occupancy_channel(connection: sqlite3.Connection) -> None:
    connection.execute("DROP TRIGGER host_occupancy_bindings_no_update")
    connection.execute("DROP TRIGGER host_occupancy_bindings_no_delete")
    connection.execute("DROP TRIGGER host_occupancy_revisions_no_update")
    connection.execute("DROP TRIGGER host_occupancy_revisions_no_delete")
    connection.execute(f"DROP TABLE {host_occupancy_bindings.name}")
    connection.execute(f"DROP TABLE {host_occupancy_revisions.name}")


def _drop_queue_items_table(connection: sqlite3.Connection) -> None:
    connection.execute("DROP TRIGGER queue_items_identity_no_update")
    connection.execute("DROP TRIGGER queue_items_no_delete")
    connection.execute(f"DROP TABLE {queue_items.name}")


def _drop_webhook_delivery_cursor_table(connection: sqlite3.Connection) -> None:
    connection.execute("DROP TRIGGER webhook_delivery_cursor_identity_no_update")
    connection.execute("DROP TRIGGER webhook_delivery_cursor_no_delete")
    connection.execute(f"DROP TABLE {webhook_delivery_cursor.name}")


def _drop_project_source_connection_table(connection: sqlite3.Connection) -> None:
    connection.execute(
        "DROP TRIGGER host_project_source_connection_revisions_no_update"
    )
    connection.execute(
        "DROP TRIGGER host_project_source_connection_revisions_no_delete"
    )
    connection.execute(f"DROP TABLE {host_project_source_connection_revisions.name}")


_PREDECESSOR_WAIT_ANSWERS_DDL = """
CREATE TABLE wait_answers (
	run_id TEXT NOT NULL, 
	revision_hash TEXT NOT NULL, 
	node_id TEXT NOT NULL, 
	node_execution_id TEXT NOT NULL, 
	answer_bytes BLOB NOT NULL, 
	answer_hash TEXT NOT NULL, 
	answer_workflow_id TEXT NOT NULL, 
	state TEXT NOT NULL, 
	state_version INTEGER NOT NULL, 
	PRIMARY KEY (run_id, node_id), 
	FOREIGN KEY(run_id, revision_hash) REFERENCES runs (run_id, revision_hash), 
	CHECK (length(node_id) > 0), 
	CHECK (length(node_execution_id) = 64 AND node_execution_id NOT GLOB '*[^0-9a-f]*'), 
	CHECK (length(answer_hash) = 64 AND answer_hash NOT GLOB '*[^0-9a-f]*'), 
	CHECK (length(answer_workflow_id) > 0), 
	CHECK (state IN ('PENDING', 'APPLIED')), 
	CHECK (state_version IN (0, 1)), 
	CHECK ((state = 'PENDING' AND state_version = 0) OR (state = 'APPLIED' AND state_version = 1)), 
	UNIQUE (node_execution_id), 
	UNIQUE (answer_workflow_id)
)
"""

_PREDECESSOR_WAIT_ANSWERS_PAYLOAD_TRIGGER_DDL = """
CREATE TRIGGER wait_answers_payload_no_update
BEFORE UPDATE OF run_id, revision_hash, node_id, node_execution_id,
                 answer_bytes, answer_hash, answer_workflow_id
ON wait_answers BEGIN
  SELECT RAISE(ABORT, 'wait answer bindings are immutable');
END
"""


_PARKED_CURRENT_WAIT_ANSWERS = "wait_answers_of_the_current_schema"


def _revert_wait_answers_execution_key(connection: sqlite3.Connection) -> None:
    """Restore the run-and-node key and roundless payload trigger the #671 hop moved.

    `wait_answers` has carried one shape since it was introduced, so every
    "exact vNN store" fixture up to V33 shares this one predecessor -- the #671
    hop is the first to touch it at all.

    Rows are carried back the way the hop carries them forward, dropping the
    round the predecessor has no column for. An empty table copies nothing, so
    the fixtures that only want the shape pay nothing for it, and a store that
    was driven to a real pause keeps the answer that pause accepted.
    """

    for trigger in _WAIT_ANSWERS_TRIGGERS:
        connection.execute(f"DROP TRIGGER {trigger}")
    connection.execute(
        f"ALTER TABLE {wait_answers.name} RENAME TO {_PARKED_CURRENT_WAIT_ANSWERS}"
    )
    connection.execute(_PREDECESSOR_WAIT_ANSWERS_DDL)
    carried = ", ".join(
        str(record[1])
        for record in connection.execute(f"PRAGMA table_info({wait_answers.name})")
    )
    connection.execute(
        f"INSERT INTO {wait_answers.name} ({carried}) "
        f"SELECT {carried} FROM {_PARKED_CURRENT_WAIT_ANSWERS}"
    )
    connection.execute(f"DROP TABLE {_PARKED_CURRENT_WAIT_ANSWERS}")
    connection.execute(_PREDECESSOR_WAIT_ANSWERS_PAYLOAD_TRIGGER_DDL)
    connection.execute(_PRODUCT_TRIGGERS["wait_answers_state_transition"])
    connection.execute(_PRODUCT_TRIGGERS["wait_answers_no_delete"])


_PARKED_CURRENT_RUN_EVENTS = "run_events_after_the_cancelled_wait"


def _revert_wait_cancelled_event_kind(connection: sqlite3.Connection) -> None:
    """Restore the event-kind vocabulary the #668 hop widened.

    `run_events` has carried one shape since the V20 round column, so every
    "exact vNN store" fixture up to V34 shares this one predecessor -- the #668
    hop is simply the first since then to touch the table again. Stored events
    are carried back the way the hop carries them forward; no predecessor row
    can hold the kind the hop adds, so nothing is left behind.
    """

    _rebuild_product_table(
        connection,
        run_events,
        _PARKED_CURRENT_RUN_EVENTS,
        _RUN_EVENTS_TRIGGERS,
        SCHEMA_VERSION,
        V34_SCHEMA_HANDOFF.version,
    )


def _revert_cancelled_run_state(connection: sqlite3.Connection) -> None:
    """Restore the pre-CANCELLED `runs` CHECK the #439 P1 hop widened.

    `runs`' shape has been unchanged since the V20 round column, so every
    "exact vNN store" fixture between V21 and V29 shares the one V20 shape --
    the #439 P1 hop is simply the first since then to touch it again.
    """

    _rebuild_product_table(
        connection,
        runs,
        "runs_after_cancelled_state",
        ("runs_binding_no_update",),
        SCHEMA_VERSION,
        _VERSION_TWENTY,
    )


def _revert_runner_evidence_attempts(connection: sqlite3.Connection) -> None:
    """Restore the exact V26 attempt table and its pre-Runner trigger."""

    _revert_the_redemption_owner(connection)

    _rebuild_product_table(
        connection,
        agent_attempts,
        "agent_attempts_after_runner_evidence",
        _AGENT_ATTEMPTS_TRIGGERS,
        SCHEMA_VERSION,
        V26_SCHEMA_HANDOFF.version,
        trigger_source=_V24_AGENT_ATTEMPT_TRIGGERS,
    )


def _revert_agent_attempts_trigger_to_v27(connection: sqlite3.Connection) -> None:
    """Restore the pre-#584 attempt trigger V27 through V31 all shared.

    The current schema (#584, V32) is the first to change
    `agent_attempts_state_transition` since V27 gave it its runner-aware form,
    so a fixture that reverts only the version -- not the attempt table -- must
    also swap this trigger back, or its shape no longer matches the published
    fingerprint for the version it claims.
    """

    _revert_the_redemption_owner(connection)

    connection.execute("DROP TRIGGER agent_attempts_state_transition")
    connection.execute(_V27_AGENT_ATTEMPT_STATE_TRANSITION)


def _create_exact_v21_store(database_path: Path) -> None:
    """A current store with instants and AGENT_REFUSED removed: the published V21 shape."""

    engine = create_canonical_engine(database_path)
    initialize_schema(engine)
    engine.dispose()
    with sqlite3.connect(database_path) as connection:
        _restore_v39_configuration_tables(connection)
        _restore_v27_access_store(connection)
        _drop_queue_items_table(connection)
        _drop_webhook_delivery_cursor_table(connection)
        _drop_project_source_connection_table(connection)
        _revert_wait_cancelled_event_kind(connection)
        _revert_wait_answers_execution_key(connection)
        _revert_cancelled_run_state(connection)
        _revert_agent_refused_attempts(connection)
        _drop_occupancy_channel(connection)
        _drop_host_project_root_channel(connection)
        for trigger in (
            "run_instants_start_no_update",
            "run_instants_end_once",
            "run_instants_no_delete",
            "attempt_instants_start_no_update",
            "attempt_instants_end_once",
            "attempt_instants_no_delete",
            "event_instants_no_update",
            "event_instants_no_delete",
        ):
            connection.execute(f"DROP TRIGGER {trigger}")
        for table in (run_instants.name, attempt_instants.name, event_instants.name):
            connection.execute(f"DROP TABLE {table}")
        _revert_the_abandoned_intent_state(connection)
        connection.execute(
            "UPDATE atelier_schema_versions SET version = ?",
            (V21_SCHEMA_HANDOFF.version,),
        )
        connection.commit()
        _require_product_shape(connection, V21_SCHEMA_HANDOFF.version)


def _create_exact_v22_store(database_path: Path) -> None:
    """A current store with AGENT_REFUSED removed: the published V22 shape."""

    engine = create_canonical_engine(database_path)
    initialize_schema(engine)
    engine.dispose()
    with sqlite3.connect(database_path) as connection:
        _restore_v39_configuration_tables(connection)
        _restore_v27_access_store(connection)
        _drop_queue_items_table(connection)
        _drop_webhook_delivery_cursor_table(connection)
        _drop_project_source_connection_table(connection)
        _revert_wait_cancelled_event_kind(connection)
        _revert_wait_answers_execution_key(connection)
        _revert_cancelled_run_state(connection)
        _revert_agent_refused_attempts(connection)
        _drop_occupancy_channel(connection)
        _drop_host_project_root_channel(connection)
        _revert_the_abandoned_intent_state(connection)
        connection.execute(
            "UPDATE atelier_schema_versions SET version = ?",
            (V22_SCHEMA_HANDOFF.version,),
        )
        connection.commit()
        _require_product_shape(connection, V22_SCHEMA_HANDOFF.version)


def _create_exact_v23_store(database_path: Path) -> None:
    """A current store with PROJECT_VERIFICATION_FAILED removed: V23."""

    engine = create_canonical_engine(database_path)
    initialize_schema(engine)
    engine.dispose()
    with sqlite3.connect(database_path) as connection:
        _restore_v39_configuration_tables(connection)
        _restore_v27_access_store(connection)
        _drop_queue_items_table(connection)
        _drop_webhook_delivery_cursor_table(connection)
        _drop_project_source_connection_table(connection)
        _revert_wait_cancelled_event_kind(connection)
        _revert_wait_answers_execution_key(connection)
        _revert_cancelled_run_state(connection)
        _revert_project_verification_failed_attempts(connection)
        _drop_occupancy_channel(connection)
        _drop_host_project_root_channel(connection)
        _revert_the_abandoned_intent_state(connection)
        connection.execute(
            "UPDATE atelier_schema_versions SET version = ?",
            (V23_SCHEMA_HANDOFF.version,),
        )
        connection.commit()
        _require_product_shape(connection, V23_SCHEMA_HANDOFF.version)


def _create_exact_v24_store(database_path: Path) -> None:
    """A current store without the host configuration channel: V24."""

    engine = create_canonical_engine(database_path)
    initialize_schema(engine)
    engine.dispose()
    with sqlite3.connect(database_path) as connection:
        _restore_v39_configuration_tables(connection)
        _restore_v27_access_store(connection)
        _drop_queue_items_table(connection)
        _drop_webhook_delivery_cursor_table(connection)
        _drop_project_source_connection_table(connection)
        _revert_wait_cancelled_event_kind(connection)
        _revert_wait_answers_execution_key(connection)
        _revert_cancelled_run_state(connection)
        _revert_runner_evidence_attempts(connection)
        _drop_occupancy_channel(connection)
        _drop_host_project_root_channel(connection)
        _revert_the_abandoned_intent_state(connection)
        connection.execute(
            "UPDATE atelier_schema_versions SET version = ?",
            (V24_SCHEMA_HANDOFF.version,),
        )
        connection.commit()
        _require_product_shape(connection, V24_SCHEMA_HANDOFF.version)


def _create_exact_v25_store(database_path: Path) -> None:
    """A current store without occupancy revisions: V25."""

    engine = create_canonical_engine(database_path)
    initialize_schema(engine)
    engine.dispose()
    with sqlite3.connect(database_path) as connection:
        _restore_v39_configuration_tables(connection)
        _restore_v27_access_store(connection)
        _drop_queue_items_table(connection)
        _drop_webhook_delivery_cursor_table(connection)
        _drop_project_source_connection_table(connection)
        _revert_wait_cancelled_event_kind(connection)
        _revert_wait_answers_execution_key(connection)
        _revert_cancelled_run_state(connection)
        _revert_runner_evidence_attempts(connection)
        _drop_occupancy_channel(connection)
        _revert_the_abandoned_intent_state(connection)
        connection.execute(
            "UPDATE atelier_schema_versions SET version = ?",
            (V25_SCHEMA_HANDOFF.version,),
        )
        connection.commit()
        _require_product_shape(connection, V25_SCHEMA_HANDOFF.version)


def _create_exact_v26_store(database_path: Path) -> None:
    """The published occupancy store before Runner evidence existed."""

    engine = create_canonical_engine(database_path)
    initialize_schema(engine)
    engine.dispose()
    with sqlite3.connect(database_path) as connection:
        _restore_v39_configuration_tables(connection)
        _restore_v27_access_store(connection)
        _drop_queue_items_table(connection)
        _drop_webhook_delivery_cursor_table(connection)
        _drop_project_source_connection_table(connection)
        _revert_wait_cancelled_event_kind(connection)
        _revert_wait_answers_execution_key(connection)
        _revert_cancelled_run_state(connection)
        _revert_runner_evidence_attempts(connection)
        _revert_the_abandoned_intent_state(connection)
        connection.execute(
            "UPDATE atelier_schema_versions SET version = ?",
            (V26_SCHEMA_HANDOFF.version,),
        )
        connection.commit()
        _require_product_shape(connection, V26_SCHEMA_HANDOFF.version)


def _insert_v27_receipt_witness(
    connection: sqlite3.Connection, *, access: bool
) -> None:
    configuration, package, request, execution = (
        "44" * 32,
        "33" * 32,
        "22" * 32,
        "11" * 32,
    )
    connection.execute(
        "INSERT INTO run_configuration_revisions VALUES (?, ?)",
        (configuration, b"frozen configuration"),
    )
    connection.execute(
        "INSERT INTO context_packages_v3 VALUES (?, ?)",
        (package, b"frozen manifest"),
    )
    connection.execute(
        "INSERT INTO node_execution_requests_v3 VALUES (?, ?, ?, ?, ?)",
        (request, execution, configuration, package, b"frozen request"),
    )
    connection.execute(
        "INSERT INTO node_receipts_v3 VALUES (?, ?, ?, ?, ?, ?)",
        (execution, "succeeded", "completed", request, package, "9f" * 32),
    )
    if access:
        connection.execute(
            "INSERT INTO node_receipt_access_v3 VALUES (?, ?, ?)",
            (execution, 0, "aa" * 32),
        )


def _create_exact_v27_store(database_path: Path, *, access: bool = False) -> None:
    engine = create_canonical_engine(database_path)
    initialize_schema(engine)
    engine.dispose()
    with sqlite3.connect(database_path) as connection:
        _restore_v39_configuration_tables(connection)
        _revert_the_attempt_transcript_pointer(connection)
        _restore_v27_access_store(connection)
        _drop_queue_items_table(connection)
        _drop_webhook_delivery_cursor_table(connection)
        _drop_project_source_connection_table(connection)
        _revert_wait_cancelled_event_kind(connection)
        _revert_wait_answers_execution_key(connection)
        _revert_cancelled_run_state(connection)
        _revert_agent_attempts_trigger_to_v27(connection)
        _revert_the_abandoned_intent_state(connection)
        connection.execute(
            "UPDATE atelier_schema_versions SET version = ?",
            (V27_SCHEMA_HANDOFF.version,),
        )
        _insert_v27_receipt_witness(connection, access=access)
        connection.commit()
        _require_product_shape(connection, V27_SCHEMA_HANDOFF.version)


def _create_exact_v28_store(database_path: Path) -> None:
    """A current store without the queue admission table: the published V28 shape."""

    engine = create_canonical_engine(database_path)
    initialize_schema(engine)
    engine.dispose()
    with sqlite3.connect(database_path) as connection:
        _restore_v39_configuration_tables(connection)
        _revert_the_attempt_transcript_pointer(connection)
        _drop_queue_items_table(connection)
        _drop_webhook_delivery_cursor_table(connection)
        _drop_project_source_connection_table(connection)
        _revert_wait_cancelled_event_kind(connection)
        _revert_wait_answers_execution_key(connection)
        _revert_cancelled_run_state(connection)
        _revert_agent_attempts_trigger_to_v27(connection)
        _revert_the_abandoned_intent_state(connection)
        connection.execute(
            "UPDATE atelier_schema_versions SET version = ?",
            (V28_SCHEMA_HANDOFF.version,),
        )
        connection.commit()
        _require_product_shape(connection, V28_SCHEMA_HANDOFF.version)


def _create_exact_v29_store(database_path: Path) -> None:
    """A current store with the pre-CANCELLED runs CHECK: the published V29 shape.

    Unlike V28's fixture, `queue_items` stays: it is the table V29 itself
    added, and this store already carries every hop up to and including it.
    """

    engine = create_canonical_engine(database_path)
    initialize_schema(engine)
    engine.dispose()
    with sqlite3.connect(database_path) as connection:
        _restore_v39_configuration_tables(connection)
        _revert_the_attempt_transcript_pointer(connection)
        _drop_webhook_delivery_cursor_table(connection)
        _drop_project_source_connection_table(connection)
        _revert_wait_cancelled_event_kind(connection)
        _revert_wait_answers_execution_key(connection)
        _revert_cancelled_run_state(connection)
        _revert_agent_attempts_trigger_to_v27(connection)
        _revert_the_abandoned_intent_state(connection)
        connection.execute(
            "UPDATE atelier_schema_versions SET version = ?",
            (V29_SCHEMA_HANDOFF.version,),
        )
        connection.commit()
        _require_product_shape(connection, V29_SCHEMA_HANDOFF.version)


def _v27_living_rows(database_path: Path) -> tuple[tuple[object, ...], ...]:
    with sqlite3.connect(database_path) as connection:
        return tuple(
            (table, *row)
            for table in (
                "run_configuration_revisions",
                "context_packages_v3",
                "node_execution_requests_v3",
                "node_receipts_v3",
            )
            for row in connection.execute(f"SELECT * FROM {table}")
        )


@pytest.mark.proves("empty-v27-access-store-migrates-with-living-rows-intact")
def test_populated_v27_with_empty_access_store_migrates_and_reopens(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database_path = tmp_path / "atelier.sqlite"
    _create_exact_v27_store(database_path)
    before = _v27_living_rows(database_path)

    assert main(["migrate", "--database", str(database_path)]) == 0

    shown = capsys.readouterr()
    assert "27" in shown.out and "28" in shown.out and "29" in shown.out
    assert _v27_living_rows(database_path) == before
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT version FROM atelier_schema_versions"
        ).fetchone() == (SCHEMA_VERSION,)
        assert (
            connection.execute(
                "SELECT name FROM sqlite_master WHERE name LIKE "
                "'node_receipt_access_v3%'"
            ).fetchall()
            == []
        )
    engine = create_canonical_engine(database_path)
    initialize_schema(engine)
    engine.dispose()


@pytest.mark.proves("nonempty-v27-access-store-is-refused-unaltered")
def test_nonempty_v27_access_store_is_refused_without_mutation(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database_path = tmp_path / "atelier.sqlite"
    _create_exact_v27_store(database_path, access=True)
    before = _logical_dump(database_path)

    assert main(["migrate", "--database", str(database_path)]) == 1

    shown = capsys.readouterr()
    assert "node_receipt_access_v3" in shown.err and "will not alter" in shown.err
    assert _logical_dump(database_path) == before


def test_nonempty_access_store_rolls_back_the_whole_v26_chain(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database_path = tmp_path / "atelier.sqlite"
    _create_exact_v26_store(database_path)
    with sqlite3.connect(database_path) as connection:
        _insert_v27_receipt_witness(connection, access=True)
        connection.commit()
    before = _logical_dump(database_path)

    assert main(["migrate", "--database", str(database_path)]) == 1

    assert "node_receipt_access_v3" in capsys.readouterr().err
    assert _logical_dump(database_path) == before
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT version FROM atelier_schema_versions"
        ).fetchone() == (26,)


def test_an_exact_v21_store_migrates_to_v22(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database_path = tmp_path / "atelier.sqlite"
    _create_exact_v21_store(database_path)
    engine = create_canonical_engine(database_path)
    with pytest.raises(MigrationRequired, match="schema version 21"):
        initialize_schema(engine)
    engine.dispose()

    assert main(["migrate", "--database", str(database_path)]) == 0

    shown = capsys.readouterr()
    assert "21" in shown.out and "22" in shown.out
    assert PRODUCT_SCHEMA_HANDOFF.fingerprint_sha256 in shown.out

    engine = create_canonical_engine(database_path)
    initialize_schema(engine)
    with engine.connect() as connection:
        assert (
            connection.scalar(sa.select(atelier_schema_versions.c.version))
            == SCHEMA_VERSION
        )
        for table in (run_instants, attempt_instants, event_instants):
            assert connection.scalar(sa.select(sa.func.count()).select_from(table)) == 0
    engine.dispose()


def test_an_exact_v22_store_migrates_to_v23(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database_path = tmp_path / "atelier.sqlite"
    _create_exact_v22_store(database_path)
    engine = create_canonical_engine(database_path)
    with pytest.raises(MigrationRequired, match="schema version 22"):
        initialize_schema(engine)
    engine.dispose()

    assert main(["migrate", "--database", str(database_path)]) == 0

    shown = capsys.readouterr()
    assert "22" in shown.out and "23" in shown.out
    assert PRODUCT_SCHEMA_HANDOFF.fingerprint_sha256 in shown.out

    engine = create_canonical_engine(database_path)
    initialize_schema(engine)
    with engine.connect() as connection:
        assert (
            connection.scalar(sa.select(atelier_schema_versions.c.version))
            == SCHEMA_VERSION
        )
    engine.dispose()


def test_an_exact_v23_store_migrates_to_v24(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database_path = tmp_path / "atelier.sqlite"
    _create_exact_v23_store(database_path)
    engine = create_canonical_engine(database_path)
    with pytest.raises(MigrationRequired, match="schema version 23"):
        initialize_schema(engine)
    engine.dispose()

    assert main(["migrate", "--database", str(database_path)]) == 0

    shown = capsys.readouterr()
    assert "23" in shown.out and "24" in shown.out
    assert PRODUCT_SCHEMA_HANDOFF.fingerprint_sha256 in shown.out

    engine = create_canonical_engine(database_path)
    initialize_schema(engine)
    with engine.connect() as connection:
        assert (
            connection.scalar(sa.select(atelier_schema_versions.c.version))
            == SCHEMA_VERSION
        )
    engine.dispose()


def test_an_exact_v24_store_migrates_to_v25(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database_path = tmp_path / "atelier.sqlite"
    _create_exact_v24_store(database_path)
    engine = create_canonical_engine(database_path)
    with pytest.raises(MigrationRequired, match="schema version 24"):
        initialize_schema(engine)
    engine.dispose()

    assert main(["migrate", "--database", str(database_path)]) == 0

    shown = capsys.readouterr()
    assert "24" in shown.out and "25" in shown.out
    assert PRODUCT_SCHEMA_HANDOFF.fingerprint_sha256 in shown.out

    engine = create_canonical_engine(database_path)
    initialize_schema(engine)
    with engine.connect() as connection:
        assert (
            connection.scalar(sa.select(atelier_schema_versions.c.version))
            == SCHEMA_VERSION
        )
        assert (
            connection.scalar(
                sa.select(sa.func.count()).select_from(host_project_root_revisions)
            )
            == 0
        )
        assert (
            connection.scalar(
                sa.select(sa.func.count()).select_from(host_model_registry_revisions)
            )
            == 0
        )
    engine.dispose()


def test_an_exact_v28_store_migrates_through_v29_to_v30(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database_path = tmp_path / "atelier.sqlite"
    _create_exact_v28_store(database_path)
    engine = create_canonical_engine(database_path)
    with pytest.raises(MigrationRequired, match="schema version 28"):
        initialize_schema(engine)
    engine.dispose()

    assert main(["migrate", "--database", str(database_path)]) == 0

    shown = capsys.readouterr()
    assert "28" in shown.out and "29" in shown.out and "30" in shown.out
    assert PRODUCT_SCHEMA_HANDOFF.fingerprint_sha256 in shown.out

    engine = create_canonical_engine(database_path)
    initialize_schema(engine)
    with engine.connect() as connection:
        assert (
            connection.scalar(sa.select(atelier_schema_versions.c.version))
            == SCHEMA_VERSION
        )
        assert (
            connection.scalar(sa.select(sa.func.count()).select_from(queue_items)) == 0
        )
    engine.dispose()


def test_an_exact_v29_store_migrates_to_v30(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database_path = tmp_path / "atelier.sqlite"
    _create_exact_v29_store(database_path)
    engine = create_canonical_engine(database_path)
    with pytest.raises(MigrationRequired, match="schema version 29"):
        initialize_schema(engine)
    engine.dispose()

    assert main(["migrate", "--database", str(database_path)]) == 0

    shown = capsys.readouterr()
    assert "29" in shown.out and "30" in shown.out
    assert PRODUCT_SCHEMA_HANDOFF.fingerprint_sha256 in shown.out

    engine = create_canonical_engine(database_path)
    initialize_schema(engine)
    with engine.connect() as connection:
        assert (
            connection.scalar(sa.select(atelier_schema_versions.c.version))
            == SCHEMA_VERSION
        )
        revision_hash = "cc" * 32
        connection.execute(
            workflow_revisions.insert().values(
                revision_hash=revision_hash, document=b"post-v30-migration"
            )
        )
        connection.execute(
            runs.insert().values(
                run_id="post-v30-run",
                bootstrap_workflow_id="post-v30-workflow",
                revision_hash=revision_hash,
                workflow_format_version=1,
                agent_binding_set_hash=None,
                current_node_id="final",
                current_round_ordinal=FIRST_ROUND_ORDINAL,
                state="CANCELLED",
                state_version=0,
                last_event_sequence=0,
                terminal_hash="0" * 64,
            )
        )
        connection.commit()
    engine.dispose()


def test_an_exact_v25_store_migrates_through_v27_and_v28_and_v29_to_v30(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database_path = tmp_path / "atelier.sqlite"
    _create_exact_v25_store(database_path)
    engine = create_canonical_engine(database_path)
    with pytest.raises(MigrationRequired, match="schema version 25"):
        initialize_schema(engine)
    engine.dispose()

    assert main(["migrate", "--database", str(database_path)]) == 0

    shown = capsys.readouterr()
    assert all(step in shown.out for step in ("25", "26", "27", "28", "29", "30"))
    assert PRODUCT_SCHEMA_HANDOFF.fingerprint_sha256 in shown.out

    engine = create_canonical_engine(database_path)
    initialize_schema(engine)
    with engine.connect() as connection:
        assert (
            connection.scalar(sa.select(atelier_schema_versions.c.version))
            == SCHEMA_VERSION
        )
        assert (
            connection.scalar(
                sa.select(sa.func.count()).select_from(host_model_registry_revisions)
            )
            == 0
        )
        assert (
            connection.scalar(
                sa.select(sa.func.count()).select_from(
                    host_project_model_defaults_revisions
                )
            )
            == 0
        )
    engine.dispose()


def test_an_exact_v26_store_migrates_through_v27_and_v28_and_v29_to_v30(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database_path = tmp_path / "atelier.sqlite"
    _create_exact_v26_store(database_path)
    engine = create_canonical_engine(database_path)
    with pytest.raises(MigrationRequired, match="schema version 26"):
        initialize_schema(engine)
    engine.dispose()

    assert main(["migrate", "--database", str(database_path)]) == 0

    shown = capsys.readouterr()
    assert all(step in shown.out for step in ("26", "27", "28", "29", "30"))
    assert PRODUCT_SCHEMA_HANDOFF.fingerprint_sha256 in shown.out
    engine = create_canonical_engine(database_path)
    initialize_schema(engine)
    with engine.connect() as connection:
        assert (
            connection.scalar(sa.select(atelier_schema_versions.c.version))
            == SCHEMA_VERSION
        )
        assert tuple(agent_attempts.c.keys())[-6:] == (
            "runner_manifest_id",
            "runner_generation_id",
            "runner_invocation_id",
            "runner_terminal_evidence_hash",
            "runner_evidence_acceptance_phase",
            _TRANSCRIPT_POINTER_COLUMN,
        )
    engine.dispose()


def test_v26_attempt_bytes_cross_v27_and_v28_unchanged_with_none_evidence(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database_path = tmp_path / "atelier.sqlite"
    assert (V27_SCHEMA_HANDOFF.version, agent_attempts.name) in PUBLISHED_TABLE_SHAPES
    runtime = attempt_runtime(tmp_path)
    runtime.initialize_storage()
    request = attempt_request(runtime, "migration/v26-populated")
    DbosAgentAttemptStore(runtime.engine).prepare(agent_attempt_execution(request))
    runtime.close()

    with sqlite3.connect(database_path) as connection:
        _restore_v39_configuration_tables(connection)
        _restore_v27_access_store(connection)
        _drop_queue_items_table(connection)
        _drop_webhook_delivery_cursor_table(connection)
        _drop_project_source_connection_table(connection)
        _revert_wait_cancelled_event_kind(connection)
        _revert_wait_answers_execution_key(connection)
        _revert_cancelled_run_state(connection)
        _revert_runner_evidence_attempts(connection)
        _revert_the_abandoned_intent_state(connection)
        connection.execute(
            "UPDATE atelier_schema_versions SET version = ?",
            (V26_SCHEMA_HANDOFF.version,),
        )
        connection.commit()
        _require_product_shape(connection, V26_SCHEMA_HANDOFF.version)
        predecessor_columns = tuple(
            str(record[1])
            for record in connection.execute("PRAGMA table_info(agent_attempts)")
        )
        predecessor_row = connection.execute("SELECT * FROM agent_attempts").fetchone()
    assert predecessor_row is not None

    assert main(["migrate", "--database", str(database_path)]) == 0
    capsys.readouterr()

    with sqlite3.connect(database_path) as connection:
        projected = ", ".join(predecessor_columns)
        assert (
            connection.execute(f"SELECT {projected} FROM agent_attempts").fetchone()
            == predecessor_row
        )
        runner_fields = connection.execute(
            "SELECT runner_manifest_id, runner_generation_id, runner_invocation_id, "
            "runner_terminal_evidence_hash, runner_evidence_acceptance_phase "
            "FROM agent_attempts"
        ).fetchone()
    assert runner_fields == (None, None, None, None, "NONE")


@pytest.mark.parametrize(
    "collision_sql",
    (
        "CREATE TABLE agent_attempts_before_runner_evidence(wrong TEXT)",
        "CREATE VIEW agent_attempts_before_runner_evidence AS SELECT 1 AS wrong",
    ),
)
def test_a_refused_v27_hop_rolls_back_the_attempt_rebuild(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    collision_sql: str,
) -> None:
    database_path = tmp_path / "atelier.sqlite"
    _create_exact_v26_store(database_path)
    with sqlite3.connect(database_path) as connection:
        connection.execute(collision_sql)
        connection.commit()
    before = _logical_dump(database_path)

    assert main(["migrate", "--database", str(database_path)]) == 1

    shown = capsys.readouterr()
    assert "agent_attempts_before_runner_evidence" in shown.err
    assert "will not alter" in shown.err
    assert _logical_dump(database_path) == before
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT version FROM atelier_schema_versions"
        ).fetchone() == (26,)


@pytest.mark.parametrize(
    "collision_sql",
    [
        pytest.param(
            "CREATE TABLE host_occupancy_bindings(wrong TEXT)",
            id="table",
        ),
        pytest.param(
            "CREATE VIEW host_occupancy_bindings AS SELECT 1 AS wrong",
            id="view",
        ),
    ],
)
def test_a_refused_occupancy_hop_rolls_back_the_first_table(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], collision_sql: str
) -> None:
    """The second occupancy object already exists, so the first create undoes.

    V25→V26 creates `host_occupancy_revisions` then `host_occupancy_bindings`
    then CAS 25→26 in one transaction. A name already holding the second
    object refuses the hop after the first table exists in that transaction.
    Rollback must leave no occupancy table, no occupancy trigger, and version 25.
    """

    database_path = tmp_path / "atelier.sqlite"
    _create_exact_v25_store(database_path)
    with sqlite3.connect(database_path) as connection:
        connection.execute(collision_sql)
        connection.commit()
    before = _logical_dump(database_path)

    assert main(["migrate", "--database", str(database_path)]) == 1

    shown = capsys.readouterr()
    assert "host_occupancy_bindings" in shown.err
    assert "will not alter" in shown.err
    assert _logical_dump(database_path) == before
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT version FROM atelier_schema_versions"
        ).fetchone() == (25,)
        names = {
            record[0]
            for record in connection.execute(
                "SELECT name FROM sqlite_master WHERE name LIKE 'host_occupancy%'"
            )
        }
        assert names == {"host_occupancy_bindings"}


@pytest.mark.proves("an-unknown-or-future-schema-is-refused-by-name")
def test_an_unknown_or_future_schema_is_refused_by_name(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database_path = tmp_path / "atelier.sqlite"
    engine = create_canonical_engine(database_path)
    initialize_schema(engine)
    with engine.begin() as connection:
        connection.execute(atelier_schema_versions.delete())
        connection.execute(
            atelier_schema_versions.insert().values(version=SCHEMA_VERSION + 1)
        )
    engine.dispose()
    before = _logical_dump(database_path)

    assert main(["migrate", "--database", str(database_path)]) == 1

    shown = capsys.readouterr()
    assert str(SCHEMA_VERSION + 1) in shown.err
    assert "will not alter" in shown.err
    assert _logical_dump(database_path) == before


@pytest.mark.proves("a-current-schema-store-is-a-named-noop")
def test_a_current_schema_store_is_a_named_noop(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database_path = tmp_path / "atelier.sqlite"
    engine = create_canonical_engine(database_path)
    initialize_schema(engine)
    engine.dispose()
    before = _logical_dump(database_path)

    assert main(["migrate", "--database", str(database_path)]) == 0

    shown = capsys.readouterr()
    assert "already current" in shown.out
    assert "nothing to migrate" in shown.out
    assert PRODUCT_SCHEMA_HANDOFF.fingerprint_sha256 in shown.out
    assert _logical_dump(database_path) == before


def test_an_older_predecessor_without_a_step_is_refused_by_name(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database_path = tmp_path / "atelier.sqlite"
    with sqlite3.connect(database_path) as connection:
        connection.executescript(
            """
            CREATE TABLE atelier_schema_versions(version INTEGER PRIMARY KEY);
            CREATE TABLE predecessor_witness(value BLOB NOT NULL);
            INSERT INTO atelier_schema_versions VALUES(12);
            INSERT INTO predecessor_witness VALUES(X'00FF');
            """
        )
    before = _logical_dump(database_path)

    assert main(["migrate", "--database", str(database_path)]) == 1

    shown = capsys.readouterr()
    assert "12" in shown.err
    assert "no migration step" in shown.err
    assert _logical_dump(database_path) == before


def test_a_locked_store_is_refused_without_mutation(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database_path = tmp_path / "atelier.sqlite"
    _create_populated_v13_store(database_path)
    before = _logical_dump(database_path)
    holder = sqlite3.connect(database_path)
    holder.execute("BEGIN IMMEDIATE")
    try:
        assert main(["migrate", "--database", str(database_path)]) == 1
    finally:
        holder.rollback()
        holder.close()
    assert "in use" in capsys.readouterr().err
    assert _logical_dump(database_path) == before


@pytest.mark.parametrize(
    "collision_sql",
    [
        pytest.param(
            "CREATE TABLE run_events_before_the_receipt_column(wrong TEXT)",
            id="table",
        ),
        pytest.param(
            "CREATE VIEW run_events_before_the_receipt_column AS SELECT 1 AS wrong",
            id="view",
        ),
    ],
)
def test_a_refused_receipt_column_hop_rolls_back_every_earlier_step(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], collision_sql: str
) -> None:
    """The last step refuses, so the two that already ran are undone with it.

    The receipt-column hop rebuilds `run_events` under a parking name, so any
    object already holding that name is a collision the hop refuses by name.
    It sits behind two completed steps in the same transaction, which is what
    makes this the whole hop's atomicity and not just this step's.
    """
    database_path = tmp_path / "atelier.sqlite"
    _create_populated_v13_store(database_path)
    with sqlite3.connect(database_path) as connection:
        connection.execute(collision_sql)
        connection.commit()
    before = _logical_dump(database_path)

    assert main(["migrate", "--database", str(database_path)]) == 1

    shown = capsys.readouterr()
    assert "run_events_before_the_receipt_column" in shown.err
    assert "will not alter" in shown.err
    assert _logical_dump(database_path) == before
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT version FROM atelier_schema_versions"
        ).fetchone() == (13,)


@pytest.mark.parametrize(
    "collision_sql",
    [
        pytest.param(
            "CREATE TABLE agent_attempts_before_the_refusal_code(wrong TEXT)",
            id="table",
        ),
        pytest.param(
            "CREATE VIEW agent_attempts_before_the_refusal_code AS SELECT 1 AS wrong",
            id="view",
        ),
    ],
)
def test_a_refused_failure_code_hop_rolls_back_every_earlier_step(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], collision_sql: str
) -> None:
    """A middle hop refuses, so the three that already ran are undone with it.

    The failure-code hop rebuilds `agent_attempts` under a parking name, so any
    object already holding that name is a collision the hop refuses by name --
    after the three earlier steps completed inside the same transaction, and
    before the hops that follow it can run at all.
    """
    database_path = tmp_path / "atelier.sqlite"
    _create_populated_v13_store(database_path)
    with sqlite3.connect(database_path) as connection:
        connection.execute(collision_sql)
        connection.commit()
    before = _logical_dump(database_path)

    assert main(["migrate", "--database", str(database_path)]) == 1

    shown = capsys.readouterr()
    assert "agent_attempts_before_the_refusal_code" in shown.err
    assert "will not alter" in shown.err
    assert _logical_dump(database_path) == before
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT version FROM atelier_schema_versions"
        ).fetchone() == (13,)


@pytest.mark.parametrize(
    "collision_sql",
    [
        pytest.param(
            "CREATE TABLE runs_before_the_round_column(wrong TEXT)",
            id="table",
        ),
        pytest.param(
            "CREATE VIEW runs_before_the_round_column AS SELECT 1 AS wrong",
            id="view",
        ),
    ],
)
def test_a_refused_round_column_hop_rolls_back_every_earlier_step(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], collision_sql: str
) -> None:
    """The round hop refuses, so every step that already ran is undone with it.

    The round hop rebuilds `runs` under a parking name, so any object already
    holding that name is a collision the hop refuses by name -- after every
    earlier step completed inside the same transaction.
    """
    database_path = tmp_path / "atelier.sqlite"
    _create_populated_v13_store(database_path)
    with sqlite3.connect(database_path) as connection:
        connection.execute(collision_sql)
        connection.commit()
    before = _logical_dump(database_path)

    assert main(["migrate", "--database", str(database_path)]) == 1

    shown = capsys.readouterr()
    assert "runs_before_the_round_column" in shown.err
    assert "will not alter" in shown.err
    assert _logical_dump(database_path) == before
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT version FROM atelier_schema_versions"
        ).fetchone() == (13,)


@pytest.mark.parametrize(
    "collision_sql",
    [
        pytest.param(
            "CREATE TABLE "
            "agent_configuration_revisions_before_workspace_tools(wrong TEXT)",
            id="table",
        ),
        pytest.param(
            "CREATE VIEW agent_configuration_revisions_before_workspace_tools "
            "AS SELECT 1 AS wrong",
            id="view",
        ),
    ],
)
def test_a_refused_capability_hop_rolls_back_every_earlier_step(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], collision_sql: str
) -> None:
    """The last hop refuses, so every step that already ran is undone with it.

    It rebuilds `agent_configuration_revisions` under a parking name, so any
    object already holding that name is a collision it refuses by name -- after
    every earlier step completed inside the same transaction.
    """
    database_path = tmp_path / "atelier.sqlite"
    _create_populated_v13_store(database_path)
    with sqlite3.connect(database_path) as connection:
        connection.execute(collision_sql)
        connection.commit()
    before = _logical_dump(database_path)

    assert main(["migrate", "--database", str(database_path)]) == 1

    shown = capsys.readouterr()
    assert "agent_configuration_revisions_before_workspace_tools" in shown.err
    assert "will not alter" in shown.err
    assert _logical_dump(database_path) == before
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT version FROM atelier_schema_versions"
        ).fetchone() == (13,)


def test_a_failed_step_leaves_the_predecessor_intact(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database_path = tmp_path / "atelier.sqlite"
    _create_populated_v13_store(database_path)
    with sqlite3.connect(database_path) as connection:
        connection.execute("CREATE TABLE run_inputs_v3(wrong TEXT)")
        connection.commit()
    before = _logical_dump(database_path)

    assert main(["migrate", "--database", str(database_path)]) == 1

    shown = capsys.readouterr()
    assert shown.err
    assert _logical_dump(database_path) == before
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT version FROM atelier_schema_versions"
        ).fetchone() == (13,)


def test_a_foreign_trigger_name_collision_is_refused_without_altering_the_store(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database_path = tmp_path / "atelier.sqlite"
    _create_populated_v13_store(database_path)
    with sqlite3.connect(database_path) as connection:
        connection.executescript(
            """
            CREATE TABLE foreign_objects(value TEXT);
            CREATE TRIGGER run_inputs_v3_no_update
            BEFORE UPDATE ON foreign_objects BEGIN
              SELECT RAISE(ABORT, 'foreign object is immutable');
            END;
            """
        )
        connection.commit()
    before_bytes = database_path.read_bytes()
    before = _logical_dump(database_path)

    assert main(["migrate", "--database", str(database_path)]) == 1

    shown = capsys.readouterr()
    assert "run_inputs_v3_no_update" in shown.err
    assert "will not alter" in shown.err
    assert database_path.read_bytes() == before_bytes
    assert _logical_dump(database_path) == before
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT version FROM atelier_schema_versions"
        ).fetchone() == (13,)


def _create_exact_v31_store(database_path: Path) -> None:
    """A current store with the pre-#584 attempt trigger: the published V31 shape.

    V31 differs from the current schema only by the never-launched runner-cancel
    branch #584 added to `agent_attempts_state_transition`; the fixture is a
    fresh store with that trigger reverted to its V31 grammar. The pinned V31
    fingerprint refuses it the moment a character drifts.
    """

    engine = create_canonical_engine(database_path)
    initialize_schema(engine)
    engine.dispose()
    with sqlite3.connect(database_path) as connection:
        _restore_v39_configuration_tables(connection)
        _revert_the_attempt_transcript_pointer(connection)
        _drop_project_source_connection_table(connection)
        _revert_wait_cancelled_event_kind(connection)
        _revert_wait_answers_execution_key(connection)
        connection.execute("DROP TRIGGER agent_attempts_state_transition")
        connection.execute(_V27_AGENT_ATTEMPT_STATE_TRANSITION)
        _revert_the_abandoned_intent_state(connection)
        connection.execute(
            "UPDATE atelier_schema_versions SET version = ?",
            (V31_SCHEMA_HANDOFF.version,),
        )
        connection.commit()
        _require_product_shape(connection, V31_SCHEMA_HANDOFF.version)


def test_an_exact_v31_store_migrates_to_v32_by_a_trigger_swap(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database_path = tmp_path / "atelier.sqlite"
    _create_exact_v31_store(database_path)
    with sqlite3.connect(database_path) as connection:
        columns_before = tuple(
            str(record[1])
            for record in connection.execute("PRAGMA table_info(agent_attempts)")
        )
        trigger_before = connection.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type='trigger' AND name='agent_attempts_state_transition'"
        ).fetchone()
    assert trigger_before is not None and "NEVER_LAUNCHED" not in trigger_before[0]

    engine = create_canonical_engine(database_path)
    with pytest.raises(MigrationRequired, match="schema version 31"):
        initialize_schema(engine)
    engine.dispose()

    assert main(["migrate", "--database", str(database_path)]) == 0
    shown = capsys.readouterr()
    assert "31" in shown.out and "32" in shown.out
    assert PRODUCT_SCHEMA_HANDOFF.fingerprint_sha256 in shown.out

    engine = create_canonical_engine(database_path)
    initialize_schema(engine)
    with engine.connect() as connection:
        assert (
            connection.scalar(sa.select(atelier_schema_versions.c.version))
            == SCHEMA_VERSION
        )
    engine.dispose()

    with sqlite3.connect(database_path) as connection:
        columns_after = tuple(
            str(record[1])
            for record in connection.execute("PRAGMA table_info(agent_attempts)")
        )
        trigger_after = connection.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type='trigger' AND name='agent_attempts_state_transition'"
        ).fetchone()
    # The V32 hop moved no table shape -- only the trigger grammar changed -- and
    # the chain does not stop there, so what the attempt table has gained by the
    # end is exactly the one column a later hop appended (#666).
    assert columns_after == (*columns_before, _TRANSCRIPT_POINTER_COLUMN)
    assert trigger_after is not None and "NEVER_LAUNCHED" in trigger_after[0]


def test_a_populated_v31_runner_attempt_survives_the_v32_trigger_swap(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A row bound to the frozen Runner's own generation/manifest columns
    (#1252: the Runner's application layer is deleted; the durable columns
    stay) survives the V32 trigger swap untouched, same as any other row."""

    database_path = tmp_path / "atelier.sqlite"
    runtime = attempt_runtime(tmp_path)
    runtime.initialize_storage()
    request = attempt_request(runtime, "migration/v31-runner-attempt")
    execution = agent_attempt_execution(request)
    store = DbosAgentAttemptStore(runtime.engine, runtime.settings.application_version)
    prepared = store.prepare(execution)
    with runtime.engine.begin() as connection:
        connection.execute(
            agent_attempts.update()
            .where(agent_attempts.c.attempt_id == execution.attempt_id.value)
            .values(
                state_version=prepared.state_version + 1,
                runner_manifest_id="a" * 64,
                runner_generation_id="runner-generation-1",
            )
        )
    durable = store.load(execution.attempt_id)
    assert durable.runner_manifest_id is not None
    assert durable.runner_invocation_id is None
    runtime.close()

    with sqlite3.connect(database_path) as connection:
        _restore_v39_configuration_tables(connection)
        _revert_the_attempt_transcript_pointer(connection)
        _drop_project_source_connection_table(connection)
        _revert_wait_cancelled_event_kind(connection)
        _revert_wait_answers_execution_key(connection)
        connection.execute("DROP TRIGGER agent_attempts_state_transition")
        connection.execute(_V27_AGENT_ATTEMPT_STATE_TRANSITION)
        _revert_the_abandoned_intent_state(connection)
        connection.execute(
            "UPDATE atelier_schema_versions SET version = ?",
            (V31_SCHEMA_HANDOFF.version,),
        )
        connection.commit()
        _require_product_shape(connection, V31_SCHEMA_HANDOFF.version)
        predecessor_columns = tuple(
            str(record[1])
            for record in connection.execute("PRAGMA table_info(agent_attempts)")
        )
        predecessor_row = connection.execute(
            f"SELECT {', '.join(predecessor_columns)} FROM agent_attempts"
        ).fetchone()
    assert predecessor_row is not None

    assert main(["migrate", "--database", str(database_path)]) == 0
    capsys.readouterr()

    with sqlite3.connect(database_path) as connection:
        assert (
            connection.execute(
                f"SELECT {', '.join(predecessor_columns)} FROM agent_attempts"
            ).fetchone()
            == predecessor_row
        )
        assert connection.execute(
            "SELECT version FROM atelier_schema_versions"
        ).fetchone() == (SCHEMA_VERSION,)


def _create_exact_v32_store(database_path: Path) -> None:
    """A current store without the connection table: the published V32 shape.

    V32 differs from the current schema only by the project-source connection
    table #567 added, so the fixture is a fresh store with that table and its
    immutability trigger pair removed. The pinned V32 fingerprint refuses it
    the moment a character drifts.
    """

    engine = create_canonical_engine(database_path)
    initialize_schema(engine)
    engine.dispose()
    with sqlite3.connect(database_path) as connection:
        _restore_v39_configuration_tables(connection)
        _revert_the_attempt_transcript_pointer(connection)
        _drop_project_source_connection_table(connection)
        _revert_wait_cancelled_event_kind(connection)
        _revert_wait_answers_execution_key(connection)
        _revert_the_abandoned_intent_state(connection)
        connection.execute(
            "UPDATE atelier_schema_versions SET version = ?",
            (V32_SCHEMA_HANDOFF.version,),
        )
        connection.commit()
        _require_product_shape(connection, V32_SCHEMA_HANDOFF.version)


def test_an_exact_v32_store_migrates_to_v33_by_adding_the_connection_table(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database_path = tmp_path / "atelier.sqlite"
    _create_exact_v32_store(database_path)
    with sqlite3.connect(database_path) as connection:
        assert (
            connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name='host_project_source_connection_revisions'"
            ).fetchone()
            is None
        )

    engine = create_canonical_engine(database_path)
    with pytest.raises(MigrationRequired, match="schema version 32"):
        initialize_schema(engine)
    engine.dispose()

    assert main(["migrate", "--database", str(database_path)]) == 0
    shown = capsys.readouterr()
    assert "32" in shown.out and "33" in shown.out
    assert PRODUCT_SCHEMA_HANDOFF.fingerprint_sha256 in shown.out

    engine = create_canonical_engine(database_path)
    initialize_schema(engine)
    with engine.connect() as connection:
        assert (
            connection.scalar(sa.select(atelier_schema_versions.c.version))
            == SCHEMA_VERSION
        )
    engine.dispose()

    with sqlite3.connect(database_path) as connection:
        trigger_names = {
            str(record[0])
            for record in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' "
                "AND tbl_name='host_project_source_connection_revisions'"
            )
        }
    assert trigger_names == {
        "host_project_source_connection_revisions_no_update",
        "host_project_source_connection_revisions_no_delete",
    }


def test_populated_v32_host_configuration_rows_survive_the_v33_table_add(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database_path = tmp_path / "atelier.sqlite"
    _create_exact_v32_store(database_path)
    engine = create_canonical_engine(database_path)
    with pytest.raises(MigrationRequired):
        initialize_schema(engine)
    engine.dispose()
    with sqlite3.connect(database_path) as connection:
        root = ProjectRootRevision(ProjectId("studio"), 1, tmp_path)
        connection.execute(
            "INSERT INTO host_project_root_revisions VALUES (?, ?, ?, ?)",
            (
                root.revision_hash.value,
                root.project_id.value,
                root.revision_number,
                str(root.root_path),
            ),
        )
        connection.commit()
        predecessor_row = connection.execute(
            "SELECT * FROM host_project_root_revisions"
        ).fetchone()
    assert predecessor_row is not None

    assert main(["migrate", "--database", str(database_path)]) == 0
    capsys.readouterr()

    with sqlite3.connect(database_path) as connection:
        assert (
            connection.execute("SELECT * FROM host_project_root_revisions").fetchone()
            == predecessor_row
        )
        assert connection.execute(
            "SELECT COUNT(*) FROM host_project_source_connection_revisions"
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT version FROM atelier_schema_versions"
        ).fetchone() == (SCHEMA_VERSION,)


@pytest.mark.parametrize(
    "collision_sql",
    [
        pytest.param(
            "CREATE TABLE host_project_source_connection_revisions(wrong TEXT)",
            id="table",
        ),
        pytest.param(
            "CREATE VIEW host_project_source_connection_revisions AS SELECT 1 AS wrong",
            id="view",
        ),
    ],
)
def test_a_refused_connection_table_hop_rolls_back_the_trigger_swap_before_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], collision_sql: str
) -> None:
    """The last step refuses, so the v31→v32 swap that already ran is undone.

    V32→V33 creates the connection table; a name already holding that object
    refuses the hop by name, after the trigger-swap step completed inside the
    same transaction. Rollback must leave version 31 and the store logically
    untouched.
    """

    database_path = tmp_path / "atelier.sqlite"
    _create_exact_v31_store(database_path)
    with sqlite3.connect(database_path) as connection:
        connection.execute(collision_sql)
        connection.commit()
    before = _logical_dump(database_path)

    assert main(["migrate", "--database", str(database_path)]) == 1

    shown = capsys.readouterr()
    assert "host_project_source_connection_revisions" in shown.err
    assert "will not alter" in shown.err
    assert _logical_dump(database_path) == before
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT version FROM atelier_schema_versions"
        ).fetchone() == (31,)


def _create_exact_v33_store(database_path: Path) -> None:
    """A current store with the pre-#671 answer table: the published V33 shape.

    V33 differs from the current schema only in `wait_answers`: keyed by run and
    node, without the round, and with a payload trigger whose column list does
    not name one. The fixture is a fresh store with that table and its payload
    trigger restored to their V33 text, and the pinned V33 fingerprint refuses
    it the moment a character drifts.
    """

    engine = create_canonical_engine(database_path)
    initialize_schema(engine)
    engine.dispose()
    with sqlite3.connect(database_path) as connection:
        _restore_v39_configuration_tables(connection)
        _revert_the_attempt_transcript_pointer(connection)
        _revert_wait_cancelled_event_kind(connection)
        _revert_wait_answers_execution_key(connection)
        _revert_the_abandoned_intent_state(connection)
        connection.execute(
            "UPDATE atelier_schema_versions SET version = ?",
            (V33_SCHEMA_HANDOFF.version,),
        )
        connection.commit()
        _require_product_shape(connection, V33_SCHEMA_HANDOFF.version)


_ANSWER_NODE_ID = "approve"


def _v33_wait_answer_values(
    run_id: RunId, revision_hash: WorkflowRevisionHash, answer_bytes: bytes
) -> tuple[str | bytes, ...]:
    """One predecessor answer row, derived from the identities production derives."""
    execution_id = NodeExecutionId.for_node(run_id, revision_hash, _ANSWER_NODE_ID)
    return (
        run_id.value,
        revision_hash.value,
        _ANSWER_NODE_ID,
        execution_id.value,
        answer_bytes,
        Sha256Hash.of(answer_bytes).value,
        answer_workflow_id_for(execution_id),
    )


def _populate_v33_wait_answers(database_path: Path) -> None:
    """One resting run holding a PENDING answer, one finished run holding an APPLIED.

    Both states have to cross the hop, because they are the two halves of the
    one thing this table exists for: an answer already written and not yet
    applied, and one whose transition already happened.
    """

    revision = WorkflowRevision(b"name: freigabe\n")
    resting_run = RunId("live/wartet-noch")
    answered_run = RunId("live/beantwortet")
    terminal_hash = Sha256Hash.of(b"the run this answer finished")
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(
            "INSERT INTO workflow_revisions (revision_hash, document) VALUES (?, ?)",
            (revision.revision_hash.value, revision.document),
        )
        connection.executemany(
            "INSERT INTO runs (run_id, bootstrap_workflow_id, revision_hash, "
            "workflow_format_version, current_node_id, current_round_ordinal, "
            "state, state_version, last_event_sequence, terminal_hash) "
            "VALUES (?, ?, ?, 1, ?, ?, ?, 1, 1, ?)",
            [
                (
                    resting_run.value,
                    f"bootstrap-{resting_run.value}",
                    revision.revision_hash.value,
                    _ANSWER_NODE_ID,
                    FIRST_ROUND_ORDINAL,
                    RunState.WAITING_INPUT.value,
                    None,
                ),
                (
                    answered_run.value,
                    f"bootstrap-{answered_run.value}",
                    revision.revision_hash.value,
                    _ANSWER_NODE_ID,
                    FIRST_ROUND_ORDINAL,
                    RunState.COMPLETED.value,
                    terminal_hash.value,
                ),
            ],
        )
        connection.executemany(
            "INSERT INTO wait_answers (run_id, revision_hash, node_id, "
            "node_execution_id, answer_bytes, answer_hash, answer_workflow_id, "
            "state, state_version) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                _v33_wait_answer_values(
                    resting_run, revision.revision_hash, b'"noch nicht"'
                )
                + (WaitAnswerState.PENDING.value, 0),
                _v33_wait_answer_values(
                    answered_run, revision.revision_hash, b'"freigegeben"'
                )
                + (WaitAnswerState.APPLIED.value, 1),
            ],
        )
        connection.commit()


def test_an_exact_v33_store_migrates_to_v34_by_rekeying_the_answer_table(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database_path = tmp_path / "atelier.sqlite"
    _create_exact_v33_store(database_path)

    engine = create_canonical_engine(database_path)
    with pytest.raises(MigrationRequired, match="schema version 33"):
        initialize_schema(engine)
    engine.dispose()

    assert main(["migrate", "--database", str(database_path)]) == 0
    shown = capsys.readouterr()
    assert "33" in shown.out and "34" in shown.out
    assert PRODUCT_SCHEMA_HANDOFF.fingerprint_sha256 in shown.out

    engine = create_canonical_engine(database_path)
    initialize_schema(engine)
    with engine.connect() as connection:
        assert (
            connection.scalar(sa.select(atelier_schema_versions.c.version))
            == SCHEMA_VERSION
        )
    engine.dispose()

    with sqlite3.connect(database_path) as connection:
        key_columns = tuple(
            str(record[1])
            for record in connection.execute("PRAGMA table_info(wait_answers)")
            if int(record[5]) > 0
        )
        parents = tuple(
            (str(record[2]), str(record[3]), str(record[4]))
            for record in connection.execute("PRAGMA foreign_key_list(wait_answers)")
        )
    assert key_columns == ("node_execution_id",)
    assert set(parents) == {
        ("runs", "run_id", "run_id"),
        ("runs", "revision_hash", "revision_hash"),
    }


def test_pending_and_applied_v33_answers_survive_the_v34_rekey_as_round_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database_path = tmp_path / "atelier.sqlite"
    _create_exact_v33_store(database_path)
    _populate_v33_wait_answers(database_path)
    with sqlite3.connect(database_path) as connection:
        predecessor_rows = connection.execute(
            "SELECT run_id, revision_hash, node_id, node_execution_id, answer_bytes, "
            "answer_hash, answer_workflow_id, state, state_version FROM wait_answers "
            "ORDER BY run_id"
        ).fetchall()
    assert len(predecessor_rows) == 2

    assert main(["migrate", "--database", str(database_path)]) == 0
    capsys.readouterr()

    with sqlite3.connect(database_path) as connection:
        carried = connection.execute(
            "SELECT run_id, revision_hash, node_id, node_execution_id, answer_bytes, "
            "answer_hash, answer_workflow_id, state, state_version FROM wait_answers "
            "ORDER BY run_id"
        ).fetchall()
        rounds = connection.execute(
            "SELECT state, round_ordinal FROM wait_answers ORDER BY run_id"
        ).fetchall()
    assert carried == predecessor_rows
    assert rounds == [
        (WaitAnswerState.APPLIED.value, FIRST_ROUND_ORDINAL),
        (WaitAnswerState.PENDING.value, FIRST_ROUND_ORDINAL),
    ]


def test_the_three_answer_triggers_are_live_again_after_the_v34_rekey(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A rebuild drops every trigger, so each one is proved by what it refuses."""

    database_path = tmp_path / "atelier.sqlite"
    _create_exact_v33_store(database_path)
    _populate_v33_wait_answers(database_path)
    assert main(["migrate", "--database", str(database_path)]) == 0
    capsys.readouterr()

    with sqlite3.connect(database_path) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="bindings are immutable"):
            connection.execute("UPDATE wait_answers SET round_ordinal = 2")
        with pytest.raises(sqlite3.IntegrityError, match="invalid wait answer"):
            connection.execute(
                "UPDATE wait_answers SET state = 'PENDING', state_version = 0 "
                "WHERE state = 'APPLIED'"
            )
        with pytest.raises(sqlite3.IntegrityError, match="answers are immutable"):
            connection.execute("DELETE FROM wait_answers")


def test_a_refused_answer_rekey_leaves_the_v33_store_untouched(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A name already holding the parking object refuses before the first statement."""

    database_path = tmp_path / "atelier.sqlite"
    _create_exact_v33_store(database_path)
    _populate_v33_wait_answers(database_path)
    with sqlite3.connect(database_path) as connection:
        connection.execute(f"CREATE TABLE {_PREDECESSOR_WAIT_ANSWERS} (wrong TEXT)")
        connection.commit()
    before = _logical_dump(database_path)

    assert main(["migrate", "--database", str(database_path)]) == 1

    shown = capsys.readouterr()
    assert _PREDECESSOR_WAIT_ANSWERS in shown.err
    assert "will not alter" in shown.err
    assert _logical_dump(database_path) == before
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT version FROM atelier_schema_versions"
        ).fetchone() == (33,)


def _enqueue_a_three_argument_answer_workflow(
    database_path: Path,
    application_version: str,
    revision_hash: WorkflowRevisionHash,
    execution_id: NodeExecutionId,
) -> None:
    """Record the answer invocation a store written before the round existed holds.

    A predecessor enqueued run, revision and node and nothing else. Recording it
    under the identity the answer will be minted with is what makes it the same
    workflow the submission would otherwise have enqueued: the submission's own
    enqueue then finds the id taken and adds nothing, so what stands in the queue
    across the hop is the three-argument shape and only that.
    """

    engine = create_canonical_engine(database_path)
    client = DBOSClient(system_database_engine=engine, use_listen_notify=False)
    try:
        options: EnqueueOptions = {
            "workflow_name": ANSWER_WORKFLOW_NAME,
            "queue_name": QUEUE_NAME,
            "workflow_id": answer_workflow_id_for(execution_id),
            "app_version": application_version,
        }
        client.enqueue(options, RUN.value, revision_hash.value, WAIT_NODE)
    finally:
        client.destroy()
        engine.dispose()


def _recorded_invocation(
    serialized: str,
) -> tuple[tuple[object, ...], dict[str, object]]:
    """Every argument DBOS really recorded, read the way DBOS reads them.

    Both halves of the call, because the positional ones alone do not say what
    the recovered workflow is handed: three positional arguments and
    `round_ordinal` as a keyword would satisfy an assertion about the tuple and
    would need no compatibility default at all. The queue row is the artifact
    under test, so it is decoded whole rather than trusted.
    """
    recorded = DefaultSerializer().deserialize(serialized)
    return tuple(recorded["args"]), dict(recorded["kwargs"])


def _downgrade_a_driven_store_to_v33(database_path: Path) -> None:
    with sqlite3.connect(database_path) as connection:
        _restore_v39_configuration_tables(connection)
        _revert_the_attempt_transcript_pointer(connection)
        _revert_wait_cancelled_event_kind(connection)
        _revert_wait_answers_execution_key(connection)
        _revert_the_abandoned_intent_state(connection)
        connection.execute(
            "UPDATE atelier_schema_versions SET version = ?",
            (V33_SCHEMA_HANDOFF.version,),
        )
        connection.commit()
        _require_product_shape(connection, V33_SCHEMA_HANDOFF.version)


def test_v45_wait_answers_gain_no_invented_actor_through_the_real_migrate_entry(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database_path = tmp_path / "atelier.sqlite"
    recording = recording_provider()
    runtime = wait_runtime_over(tmp_path, recording)
    runtime.initialize_storage()
    try:
        workflow = start_and_launch(runtime, WAIT_IN_THE_MIDDLE)
        wait_for_state(runtime, RunState.WAITING_INPUT)
        accepted = answer_wait_result(
            RUN,
            workflow.revision_hash,
            WAIT_NODE,
            NodeExecutionId.for_node(RUN, workflow.revision_hash, WAIT_NODE),
            WaitAnswerActor.OPERATOR,
            ANSWER,
            DbosWaitAnswerer(runtime.engine, runtime.settings.application_version),
        )
        assert isinstance(accepted, AnswerAcceptedPending), accepted
    finally:
        runtime.close()

    with sqlite3.connect(database_path) as connection:
        _restore_v45_answer_attribution_predecessors(connection)
        connection.execute(
            "UPDATE atelier_schema_versions SET version = ?",
            (V45_SCHEMA_HANDOFF.version,),
        )
        connection.commit()
        _require_product_shape(connection, V45_SCHEMA_HANDOFF.version)
        predecessor = connection.execute(
            "SELECT * FROM wait_answers ORDER BY node_execution_id"
        ).fetchall()

    assert V45_SCHEMA_HANDOFF.fingerprint_sha256 == (
        "39d0811369f0b7a4b248448042623ecde0d290e95d191d75c32a9faf538fffa5"
    )
    assert main(["migrate", "--database", str(database_path)]) == 0
    shown = capsys.readouterr().out
    assert "45" in shown and "46" in shown

    with sqlite3.connect(database_path) as connection:
        _require_product_shape(connection, SCHEMA_VERSION)
        migrated = connection.execute(
            "SELECT run_id, revision_hash, node_id, node_execution_id, "
            "round_ordinal, answer_bytes, answer_hash, answer_workflow_id, state, "
            "state_version FROM wait_answers ORDER BY node_execution_id"
        ).fetchall()
        actors = connection.execute(
            "SELECT actor, actor_attribution_kind FROM wait_answers "
            "ORDER BY node_execution_id"
        ).fetchall()
        waiting_actors = connection.execute(
            "SELECT wait_answer_actor FROM run_events "
            "WHERE event_kind = 'WAITING_INPUT'"
        ).fetchall()
        assert migrated == predecessor
        assert actors == [(None, "LEGACY_UNATTRIBUTED")]
        assert waiting_actors == [(WaitAnswerActor.OPERATOR.value,)]
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute("UPDATE wait_answers SET actor = 'operator'")

    recovered = wait_runtime_over(tmp_path, recording)
    try:
        recovered.launch()
        wait_for_state(recovered, RunState.COMPLETED)
        found = durable_queries(recovered.engine).get_run(RUN)
        assert isinstance(found, RunFound), found
        assert found.projection.run.state is RunState.COMPLETED

        client = public_client(recovered)
        public_reference = encode_public_run_reference(RUN)
        run_response = client.get(f"/atelier/api/v1/runs/{public_reference}")
        assert run_response.status_code == 200, run_response.text
        assert run_response.json()["state"] == RunState.COMPLETED.value
        event_response = client.get(f"/atelier/api/v1/runs/{public_reference}/events")
        assert event_response.status_code == 200, event_response.text
        event_frames = [
            json.loads(line.removeprefix("data: "))
            for line in event_response.text.splitlines()
            if line.startswith("data: ")
        ]
        answered = [
            frame for frame in event_frames if frame.get("event") == "WAIT_ANSWERED"
        ]
        assert len(answered) == 1
        assert answered[0]["actor"] == "legacy-unattributed"
        with recovered.engine.connect() as connection:
            migrated_answer = connection.execute(
                sa.select(
                    wait_answers.c.actor,
                    wait_answers.c.actor_attribution_kind,
                    wait_answers.c.state,
                )
            ).one()
        assert migrated_answer == (
            None,
            "LEGACY_UNATTRIBUTED",
            WaitAnswerState.APPLIED.value,
        )
    finally:
        recovered.close()

    replay = migrate_store(database_path)
    assert (replay.source_version, replay.target_version) == (
        SCHEMA_VERSION,
        SCHEMA_VERSION,
    )


def test_v46_failure_after_version_cas_restores_the_exact_v45_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = tmp_path / "atelier.sqlite"
    engine = create_canonical_engine(database_path)
    initialize_schema(engine)
    engine.dispose()
    with sqlite3.connect(database_path) as connection:
        _restore_v45_answer_attribution_predecessors(connection)
        connection.execute("UPDATE atelier_schema_versions SET version = 45")
        connection.commit()
        _require_product_shape(connection, 45)
    before = _logical_dump(database_path)
    raise_declared_version = schema_module._raise_declared_version

    def fail_after_version_cas(
        connection: sqlite3.Connection, expected_version: int, target_version: int
    ) -> None:
        raise_declared_version(connection, expected_version, target_version)
        if expected_version == 45:
            raise RuntimeError("v46 failpoint")

    monkeypatch.setattr(
        schema_module, "_raise_declared_version", fail_after_version_cas
    )

    with pytest.raises(RuntimeError, match="v46 failpoint"):
        migrate_store(database_path)

    assert _logical_dump(database_path) == before


def test_v46_store_migrates_to_the_current_schema_with_immutable_catalog_intakes(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "atelier.sqlite"
    engine = create_canonical_engine(database_path)
    initialize_schema(engine)
    with engine.begin() as connection:
        connection.execute(
            workflow_revisions.insert().values(revision_hash="a" * 64, document=b"kept")
        )
    engine.dispose()
    with sqlite3.connect(database_path) as connection:
        _restore_v45_answer_attribution_predecessors(connection)
        connection.execute("UPDATE atelier_schema_versions SET version = 45")
        schema_module._apply_v45_to_v46(connection)
        connection.commit()
        _require_product_shape(connection, 46)

    report = migrate_store(database_path)

    assert (report.source_version, report.target_version) == (46, SCHEMA_VERSION)
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT document FROM workflow_revisions"
        ).fetchone() == (b"kept",)
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='catalog_intakes'"
        ).fetchone()
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' AND name='catalog_intakes_no_update'"
        ).fetchone()
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' AND name='catalog_intakes_no_delete'"
        ).fetchone()
        _require_product_shape(connection, SCHEMA_VERSION)


def test_a_v33_answer_enqueued_without_a_round_still_applies_after_the_v34_hop(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The answer a predecessor accepted is applied by the runtime that comes after.

    This is the sentence the hop has to earn. An operator answered on the old
    schema; the process holding the run died before the answer workflow ran; the
    store was migrated offline; a new process came up. Nothing about that
    sequence is arranged after the fact -- the answer workflow is recorded with
    the three arguments a predecessor really wrote, the first runtime is closed
    while the answer is still PENDING and nothing is left to consume it, and the
    store is put back into its exact published V33 shape, fingerprint and all,
    with that PENDING row inside it.

    What is then asserted is that the answer is not stranded: it becomes APPLIED
    in the first round, writes exactly one WAIT_ANSWERED event, and carries the
    line on to the heir its author declared -- run by a runtime that never saw a
    byte of it in memory.
    """

    database_path = tmp_path / "atelier.sqlite"
    recording = recording_provider()
    paused = wait_runtime_over(tmp_path, recording)
    paused.initialize_storage()
    try:
        workflow = start_and_launch(paused, WAIT_IN_THE_MIDDLE)
        wait_for_state(paused, RunState.WAITING_INPUT)
        application_version = paused.settings.application_version
    finally:
        paused.close()

    execution_id = NodeExecutionId.for_node(RUN, workflow.revision_hash, WAIT_NODE)
    _enqueue_a_three_argument_answer_workflow(
        database_path, application_version, workflow.revision_hash, execution_id
    )
    engine = create_canonical_engine(database_path)
    try:
        accepted = answer_wait_result(
            RUN,
            workflow.revision_hash,
            WAIT_NODE,
            NodeExecutionId.for_node(RUN, workflow.revision_hash, WAIT_NODE),
            WaitAnswerActor.OPERATOR,
            ANSWER,
            DbosWaitAnswerer(engine, application_version),
        )
    finally:
        engine.dispose()
    assert isinstance(accepted, AnswerAcceptedPending), accepted

    _downgrade_a_driven_store_to_v33(database_path)
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT state FROM wait_answers WHERE node_execution_id = ?",
            (execution_id.value,),
        ).fetchone() == (WaitAnswerState.PENDING.value,)
        recorded_inputs = connection.execute(
            "SELECT inputs FROM workflow_status WHERE workflow_uuid = ?",
            (answer_workflow_id_for(execution_id),),
        ).fetchone()
    assert recorded_inputs is not None
    assert _recorded_invocation(str(recorded_inputs[0])) == (
        (RUN.value, workflow.revision_hash.value, WAIT_NODE),
        {},
    )

    assert main(["migrate", "--database", str(database_path)]) == 0
    capsys.readouterr()

    recovered = wait_runtime_over(tmp_path, recording)
    try:
        recovered.launch()
        wait_for_state(recovered, RunState.COMPLETED)
        with recovered.engine.connect() as connection:
            stored = connection.execute(sa.select(wait_answers)).mappings().one()
            answered = connection.execute(
                sa.select(run_events.c.node_id, run_events.c.round_ordinal).where(
                    run_events.c.event_kind == RunEventKind.WAIT_ANSWERED.value
                )
            ).all()
            heirs = (
                connection.execute(
                    sa.select(run_events.c.node_id).where(
                        run_events.c.event_kind == RunEventKind.AGENT_COMPLETED.value
                    )
                )
                .scalars()
                .all()
            )
    finally:
        recovered.close()

    assert str(stored["state"]) == WaitAnswerState.APPLIED.value
    assert int(stored["round_ordinal"]) == FIRST_ROUND_ORDINAL
    assert bytes(stored["answer_bytes"]) == ANSWER
    assert answered == [(WAIT_NODE, FIRST_ROUND_ORDINAL)]
    assert list(heirs) == ["implement", "review"]


def _create_exact_v34_store(database_path: Path) -> None:
    """A current store with the pre-#668 event vocabulary: the published V34 shape.

    V34 differs from the current schema only in `run_events`, whose kind CHECK
    does not yet name `WAIT_CANCELLED`. The fixture is a fresh store with that
    table rebuilt into its V34 text, and the pinned V34 fingerprint refuses it
    the moment a character drifts.
    """

    engine = create_canonical_engine(database_path)
    initialize_schema(engine)
    engine.dispose()
    with sqlite3.connect(database_path) as connection:
        _restore_v39_configuration_tables(connection)
        _revert_the_attempt_transcript_pointer(connection)
        _revert_wait_cancelled_event_kind(connection)
        _revert_the_abandoned_intent_state(connection)
        connection.execute(
            "UPDATE atelier_schema_versions SET version = ?",
            (V34_SCHEMA_HANDOFF.version,),
        )
        connection.commit()
        _require_product_shape(connection, V34_SCHEMA_HANDOFF.version)


_PAUSED_RUN = RunId("live/haelt-am-tor")
_PAUSED_NODE = "vorbereiten"


_ACTION_NODE = "wirken"
_FIRST_ATTEMPT = AgentAttemptId("aa" * 32)
_REPLACEMENT_ATTEMPT = AgentAttemptId("bb" * 32)
_CANCEL_COMMAND = "cancel/erste-fassung"
_EFFECT_KEY = LogicalEffectKey("wirken/einmal")
_EFFECT_RESULT = b'{"outcome":"CONFIRMED"}'


def _paused_run_event_log(revision_hash: WorkflowRevisionHash) -> tuple[RunEvent, ...]:
    """The event log one paused run really holds, derived by the contract.

    One event of every family the table has optional columns for -- an attempt
    cancelled and replaced, a completion carrying its agent receipt, an action
    bound to its effect receipt, and the pause the run rests at -- because a hop
    is only proved to carry a column by a row that had something in it. The
    hashes are the ones production would frame; nothing here recomputes them a
    second way.
    """

    node_execution = NodeExecutionId.for_node(_PAUSED_RUN, revision_hash, _PAUSED_NODE)
    return (
        RunEvent(
            _PAUSED_RUN,
            revision_hash,
            1,
            _PAUSED_NODE,
            node_execution,
            RunEventKind.AGENT_CANCEL_REQUESTED,
            b'"abgebrochen"',
            attempt_binding=RunEventCancellationBinding(
                _FIRST_ATTEMPT,
                AGENT_ATTEMPT_ORDINAL,
                AgentAttemptReplacement.ONE,
                _CANCEL_COMMAND,
            ),
        ),
        RunEvent(
            _PAUSED_RUN,
            revision_hash,
            2,
            _PAUSED_NODE,
            node_execution,
            RunEventKind.AGENT_CANCELLED,
            b'"aufgeraeumt"',
            attempt_binding=RunEventCancellationBinding(
                _FIRST_ATTEMPT,
                AGENT_ATTEMPT_ORDINAL,
                AgentAttemptReplacement.ONE,
                _CANCEL_COMMAND,
                AgentAttemptCancellationDisposition.REAPED_AFTER_TERM,
                _REPLACEMENT_ATTEMPT,
            ),
        ),
        RunEvent(
            _PAUSED_RUN,
            revision_hash,
            3,
            _PAUSED_NODE,
            node_execution,
            RunEventKind.AGENT_COMPLETED,
            b'"fertig"',
            attempt_binding=RunEventAgentAttemptBinding(
                _REPLACEMENT_ATTEMPT, REPLACEMENT_AGENT_ATTEMPT_ORDINAL
            ),
            agent_receipt_hash=AgentReceiptHash.of(b'"fertig"'),
        ),
        RunEvent(
            _PAUSED_RUN,
            revision_hash,
            4,
            _ACTION_NODE,
            NodeExecutionId.for_node(_PAUSED_RUN, revision_hash, _ACTION_NODE),
            RunEventKind.ACTION_COMPLETED,
            _EFFECT_RESULT,
            receipt_logical_key=_EFFECT_KEY,
            receipt_result_hash=Sha256Hash.of(_EFFECT_RESULT),
        ),
        RunEvent(
            _PAUSED_RUN,
            revision_hash,
            5,
            _ANSWER_NODE_ID,
            NodeExecutionId.for_node(_PAUSED_RUN, revision_hash, _ANSWER_NODE_ID),
            RunEventKind.WAITING_INPUT,
            b"",
            wait_answer_actor=WaitAnswerActor.OPERATOR,
        ),
    )


_EVENT_COLUMNS = tuple(
    column.name for column in run_events.columns if column.name != "wait_answer_actor"
)
"""Every column the event table has, in its own order.

Read from the table rather than listed here, so a column a later hop adds is
carried by this fixture and compared by it without anybody remembering to.
"""

_INSERT_EVENT_STATEMENT = (
    f"INSERT INTO run_events ({', '.join(_EVENT_COLUMNS)}) "
    f"VALUES ({', '.join('?' for _ in _EVENT_COLUMNS)})"
)


def _event_row(event: RunEvent) -> tuple[object, ...]:
    binding = event.attempt_binding
    cancellation = binding if isinstance(binding, RunEventCancellationBinding) else None
    written: Mapping[str, object] = {
        "run_id": event.run_id.value,
        "revision_hash": event.revision_hash.value,
        "event_sequence": event.event_sequence,
        "node_id": event.node_id,
        "node_execution_id": event.node_execution_id.value,
        "round_ordinal": event.round_ordinal,
        "event_kind": event.event_kind.value,
        "payload": event.payload,
        "payload_hash": event.payload_hash.value,
        "receipt_logical_key": (
            None
            if event.receipt_logical_key is None
            else event.receipt_logical_key.value
        ),
        "receipt_result_hash": (
            None
            if event.receipt_result_hash is None
            else event.receipt_result_hash.value
        ),
        "event_hash": event.event_hash.value,
        "agent_attempt_id": None if binding is None else binding.attempt_id.value,
        "attempt_ordinal": None if binding is None else binding.attempt_ordinal,
        "cancellation_command_id": (
            None if cancellation is None else cancellation.command_id
        ),
        "replacement": (
            None if cancellation is None else cancellation.replacement.value
        ),
        "cancellation_disposition": (
            None
            if cancellation is None or cancellation.disposition is None
            else cancellation.disposition.value
        ),
        "replacement_attempt_id": (
            None
            if cancellation is None or cancellation.replacement_attempt_id is None
            else cancellation.replacement_attempt_id.value
        ),
        "agent_receipt_hash": (
            None if event.agent_receipt_hash is None else event.agent_receipt_hash.value
        ),
    }
    return tuple(written[name] for name in _EVENT_COLUMNS)


def _populate_paused_run_events(database_path: Path) -> WorkflowRevisionHash:
    """One run resting at its pause, with the events that carried it there."""

    revision = WorkflowRevision(b"name: torwaechter\n")
    events = _paused_run_event_log(revision.revision_hash)
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(
            "INSERT INTO workflow_revisions (revision_hash, document) VALUES (?, ?)",
            (revision.revision_hash.value, revision.document),
        )
        connection.execute(
            "INSERT INTO runs (run_id, bootstrap_workflow_id, revision_hash, "
            "workflow_format_version, current_node_id, current_round_ordinal, "
            "state, state_version, last_event_sequence, terminal_hash) "
            "VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?, NULL)",
            (
                _PAUSED_RUN.value,
                f"bootstrap-{_PAUSED_RUN.value}",
                revision.revision_hash.value,
                _ANSWER_NODE_ID,
                FIRST_ROUND_ORDINAL,
                RunState.WAITING_INPUT.value,
                len(events),
                len(events),
            ),
        )
        _seed_effect_receipt(connection, revision.revision_hash)
        connection.executemany(
            _INSERT_EVENT_STATEMENT, [_event_row(event) for event in events]
        )
        connection.commit()
    return revision.revision_hash


def _seed_effect_receipt(
    connection: sqlite3.Connection, revision_hash: WorkflowRevisionHash
) -> None:
    """The receipt an ACTION_COMPLETED event points at, so its binding resolves.

    A migration checks foreign keys before it commits, so an event carrying a
    receipt key nothing answers would refuse the whole hop rather than prove it.
    """

    request_hash = Sha256Hash.of(b"wirken/anfrage").value
    shared = (
        _EFFECT_KEY.value,
        _PAUSED_RUN.value,
        b"wirken/anfrage",
        request_hash,
        revision_hash.value,
        "loopback-v1",
        "loopback-test",
        "operational/loopback",
    )
    connection.execute(
        "INSERT INTO effect_intents (logical_key, run_id, canonical_request, "
        "request_hash, workflow_revision_hash, adapter_revision, "
        "destination_identity, adapter_operational_identity, state, state_version) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)",
        (*shared, EffectIntentState.CONFIRMED.value),
    )
    connection.execute(
        "INSERT INTO effect_receipts (logical_key, run_id, canonical_request, "
        "request_hash, workflow_revision_hash, adapter_revision, "
        "destination_identity, adapter_operational_identity, effect_id, result, "
        "result_hash, confirmation_source) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            *shared,
            "effect/einmal",
            _EFFECT_RESULT,
            Sha256Hash.of(_EFFECT_RESULT).value,
            ConfirmationSource.ADAPTER_EXECUTION.value,
        ),
    )


def _stored_event_rows(database_path: Path) -> list[tuple[object, ...]]:
    with sqlite3.connect(database_path) as connection:
        return connection.execute(
            f"SELECT {', '.join(_EVENT_COLUMNS)} FROM run_events "
            "ORDER BY run_id, event_sequence"
        ).fetchall()


def test_an_exact_v34_store_migrates_to_v35_by_widening_the_event_vocabulary(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database_path = tmp_path / "atelier.sqlite"
    _create_exact_v34_store(database_path)

    engine = create_canonical_engine(database_path)
    with pytest.raises(MigrationRequired, match="schema version 34"):
        initialize_schema(engine)
    engine.dispose()

    assert main(["migrate", "--database", str(database_path)]) == 0
    shown = capsys.readouterr()
    assert "34" in shown.out and "35" in shown.out
    assert PRODUCT_SCHEMA_HANDOFF.fingerprint_sha256 in shown.out

    engine = create_canonical_engine(database_path)
    initialize_schema(engine)
    with engine.connect() as connection:
        assert (
            connection.scalar(sa.select(atelier_schema_versions.c.version))
            == SCHEMA_VERSION
        )
    engine.dispose()


def test_every_v34_event_crosses_the_v35_rebuild_unchanged(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The hop widens what may be written; it rewrites nothing already written."""

    database_path = tmp_path / "atelier.sqlite"
    _create_exact_v34_store(database_path)
    revision_hash = _populate_paused_run_events(database_path)
    predecessor_rows = _stored_event_rows(database_path)
    assert predecessor_rows == [
        _event_row(event) for event in _paused_run_event_log(revision_hash)
    ]

    assert main(["migrate", "--database", str(database_path)]) == 0
    capsys.readouterr()

    assert _stored_event_rows(database_path) == predecessor_rows


def test_a_v35_store_admits_the_wait_cancellation_its_predecessor_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The one sentence the hop exists for, asked of both sides of it.

    Written straight at the table rather than through the store, because what is
    under test here is the CHECK the hop moved -- the store path that mints this
    event is proved where the run is actually driven.
    """

    database_path = tmp_path / "atelier.sqlite"
    _create_exact_v34_store(database_path)
    revision_hash = _populate_paused_run_events(database_path)
    cancellation = RunEvent(
        _PAUSED_RUN,
        revision_hash,
        len(_paused_run_event_log(revision_hash)) + 1,
        _ANSWER_NODE_ID,
        NodeExecutionId.for_node(_PAUSED_RUN, revision_hash, _ANSWER_NODE_ID),
        RunEventKind.WAIT_CANCELLED,
        RunCancelCommandId.for_key("operator-key").value.encode("utf-8"),
    )

    with (
        sqlite3.connect(database_path) as connection,
        pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"),
    ):
        connection.execute(_INSERT_EVENT_STATEMENT, _event_row(cancellation))

    assert main(["migrate", "--database", str(database_path)]) == 0
    capsys.readouterr()

    with sqlite3.connect(database_path) as connection:
        connection.execute(_INSERT_EVENT_STATEMENT, _event_row(cancellation))
        connection.commit()
    assert _stored_event_rows(database_path)[-1] == _event_row(cancellation)


def test_the_event_log_is_append_only_again_after_the_v35_rebuild(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A rebuild drops every trigger and index, so each is proved by what it refuses.

    An event log that could be updated, deleted, or made to hold two entries of
    one kind for one execution would be no evidence at all, and a terminal hash
    folded over it would be a hash over something that can still change.
    """

    database_path = tmp_path / "atelier.sqlite"
    _create_exact_v34_store(database_path)
    revision_hash = _populate_paused_run_events(database_path)
    assert main(["migrate", "--database", str(database_path)]) == 0
    capsys.readouterr()
    pause_again = _pause_at_sequence(revision_hash, _UNTAKEN_SEQUENCE)

    with sqlite3.connect(database_path) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="events are immutable"):
            connection.execute("UPDATE run_events SET node_id = 'anders'")
        with pytest.raises(sqlite3.IntegrityError, match="events are immutable"):
            connection.execute("DELETE FROM run_events")
        with pytest.raises(sqlite3.IntegrityError, match="UNIQUE constraint failed"):
            connection.execute(_INSERT_EVENT_STATEMENT, _event_row(pause_again))


def test_a_refused_event_vocabulary_hop_leaves_the_v34_store_untouched(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A name already holding the parking object refuses before the first statement."""

    database_path = tmp_path / "atelier.sqlite"
    _create_exact_v34_store(database_path)
    _populate_paused_run_events(database_path)
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            f"CREATE TABLE {_PREDECESSOR_WAIT_UNCANCELLABLE_RUN_EVENTS} (wrong TEXT)"
        )
        connection.commit()
    before = _logical_dump(database_path)

    assert main(["migrate", "--database", str(database_path)]) == 1

    shown = capsys.readouterr()
    assert _PREDECESSOR_WAIT_UNCANCELLABLE_RUN_EVENTS in shown.err
    assert "will not alter" in shown.err
    assert _logical_dump(database_path) == before
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT version FROM atelier_schema_versions"
        ).fetchone() == (34,)


_PARKED_CURRENT_RUN_EVENTS_V36 = "run_events_after_the_round_scoped_key"


def _revert_the_round_scoped_event_key(connection: sqlite3.Connection) -> None:
    """Restore the once-per-node event key the #658 hop re-scoped to the round.

    Every schema up to V35 keyed an attempt-free event by its node and run at
    once, so a store that claims one of those versions has to hold that key
    again -- rebuilt from the shape and the index set V35 published, which is
    also what proves those two records are what a V35 store really was.
    """

    _rebuild_product_table(
        connection,
        run_events,
        _PARKED_CURRENT_RUN_EVENTS_V36,
        _RUN_EVENTS_TRIGGERS,
        SCHEMA_VERSION,
        V35_SCHEMA_HANDOFF.version,
    )


def _create_exact_v35_store(database_path: Path) -> None:
    """A current store keyed the pre-#658 way: the published V35 shape.

    V35 differs from the current schema in the scope of one index -- no column,
    no CHECK, no trigger -- and the pinned V35 fingerprint refuses the fixture
    the moment anything else about it drifts.
    """

    engine = create_canonical_engine(database_path)
    initialize_schema(engine)
    engine.dispose()
    with sqlite3.connect(database_path) as connection:
        _restore_v39_configuration_tables(connection)
        _revert_the_attempt_transcript_pointer(connection)
        _revert_the_round_scoped_event_key(connection)
        _revert_the_abandoned_intent_state(connection)
        connection.execute(
            "UPDATE atelier_schema_versions SET version = ?",
            (V35_SCHEMA_HANDOFF.version,),
        )
        connection.commit()
        _require_product_shape(connection, V35_SCHEMA_HANDOFF.version)


_UNTAKEN_SEQUENCE = 9
"""An event sequence past every one the seeded log took.

A duplicate has to be written at a free primary key, or the key it collides on
would be the run's own (run, sequence) rather than the one under test.
"""


def _pause_at_sequence(
    revision_hash: WorkflowRevisionHash,
    event_sequence: int,
    round_ordinal: int = FIRST_ROUND_ORDINAL,
) -> RunEvent:
    """The pause the wait node writes in one round, at one place in the log."""

    return RunEvent(
        _PAUSED_RUN,
        revision_hash,
        event_sequence,
        _ANSWER_NODE_ID,
        NodeExecutionId.for_node(
            _PAUSED_RUN, revision_hash, _ANSWER_NODE_ID, round_ordinal
        ),
        RunEventKind.WAITING_INPUT,
        b"",
        round_ordinal=round_ordinal,
        wait_answer_actor=WaitAnswerActor.OPERATOR,
    )


def test_an_exact_v35_store_migrates_to_v36_by_rescoping_the_event_key(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database_path = tmp_path / "atelier.sqlite"
    _create_exact_v35_store(database_path)

    engine = create_canonical_engine(database_path)
    with pytest.raises(MigrationRequired, match="schema version 35"):
        initialize_schema(engine)
    engine.dispose()

    assert main(["migrate", "--database", str(database_path)]) == 0
    shown = capsys.readouterr()
    assert "35" in shown.out and "36" in shown.out
    assert PRODUCT_SCHEMA_HANDOFF.fingerprint_sha256 in shown.out

    engine = create_canonical_engine(database_path)
    initialize_schema(engine)
    with engine.connect() as connection:
        assert (
            connection.scalar(sa.select(atelier_schema_versions.c.version))
            == SCHEMA_VERSION
        )
    engine.dispose()


def test_every_v35_event_column_crosses_the_v36_hop_unchanged(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The hop moves a key; it reads and writes no row at all.

    Every column of every row is compared, over a log carrying one event of each
    family the table keeps optional columns for -- a hop that lost a receipt
    binding, a cancellation disposition or a replacement attempt would otherwise
    pass on rows that never had one.
    """

    database_path = tmp_path / "atelier.sqlite"
    _create_exact_v35_store(database_path)
    revision_hash = _populate_paused_run_events(database_path)
    predecessor_rows = _stored_event_rows(database_path)
    assert predecessor_rows == [
        _event_row(event) for event in _paused_run_event_log(revision_hash)
    ]
    assert all(
        any(row[_EVENT_COLUMNS.index(column)] is not None for row in predecessor_rows)
        for column in (
            "receipt_logical_key",
            "receipt_result_hash",
            "agent_attempt_id",
            "attempt_ordinal",
            "cancellation_command_id",
            "replacement",
            "cancellation_disposition",
            "replacement_attempt_id",
            "agent_receipt_hash",
        )
    )

    assert main(["migrate", "--database", str(database_path)]) == 0
    capsys.readouterr()

    assert _stored_event_rows(database_path) == predecessor_rows


def test_a_v36_store_admits_the_second_pause_its_predecessor_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The one sentence the hop exists for, asked of both sides of it.

    Written straight at the table rather than through the store, because what is
    under test here is the key the hop re-scoped -- the run that actually turns a
    loop through two pauses is driven in `tests/integration/test_v3_wait_in_loop`.
    """

    database_path = tmp_path / "atelier.sqlite"
    _create_exact_v35_store(database_path)
    revision_hash = _populate_paused_run_events(database_path)
    second_pause = _pause_at_sequence(
        revision_hash, _UNTAKEN_SEQUENCE, FIRST_ROUND_ORDINAL + 1
    )

    with (
        sqlite3.connect(database_path) as connection,
        pytest.raises(sqlite3.IntegrityError, match="UNIQUE constraint failed"),
    ):
        connection.execute(_INSERT_EVENT_STATEMENT, _event_row(second_pause))

    assert main(["migrate", "--database", str(database_path)]) == 0
    capsys.readouterr()

    with sqlite3.connect(database_path) as connection:
        connection.execute(_INSERT_EVENT_STATEMENT, _event_row(second_pause))
        connection.commit()
    assert _stored_event_rows(database_path)[-1] == _event_row(second_pause)


def test_one_round_still_holds_one_pause_after_the_v36_hop(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """What the hop widens is which round may repeat a kind, never whether one may.

    The successor key is asked in the coordinates a reader asks in -- run,
    revision, node, round -- so a second pause of the round already stored is
    refused even when it names another execution, which is the state
    `_existing_event` would otherwise read back as two rows for one round.
    """

    database_path = tmp_path / "atelier.sqlite"
    _create_exact_v35_store(database_path)
    revision_hash = _populate_paused_run_events(database_path)
    assert main(["migrate", "--database", str(database_path)]) == 0
    capsys.readouterr()
    pause_again = _pause_at_sequence(revision_hash, _UNTAKEN_SEQUENCE)
    foreign_execution = _event_row(pause_again)
    foreign_execution = (
        foreign_execution[: _EVENT_COLUMNS.index("node_execution_id")]
        + ("f" * 64,)
        + foreign_execution[_EVENT_COLUMNS.index("node_execution_id") + 1 :]
    )

    with sqlite3.connect(database_path) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="events are immutable"):
            connection.execute("UPDATE run_events SET node_id = 'anders'")
        with pytest.raises(sqlite3.IntegrityError, match="events are immutable"):
            connection.execute("DELETE FROM run_events")
        with pytest.raises(sqlite3.IntegrityError, match="UNIQUE constraint failed"):
            connection.execute(_INSERT_EVENT_STATEMENT, _event_row(pause_again))
        with pytest.raises(sqlite3.IntegrityError, match="run_events.round_ordinal"):
            connection.execute(_INSERT_EVENT_STATEMENT, foreign_execution)


def test_a_refused_key_rescope_takes_its_own_first_statement_back(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A foreign object under the successor's name refuses the hop, and nothing is half done.

    The hop drops one index before it creates the other, so this is the case
    that proves the two statements and the version stand or fall together: the
    predecessor's key is back afterwards and the store still reads V35.
    """

    database_path = tmp_path / "atelier.sqlite"
    _create_exact_v35_store(database_path)
    _populate_paused_run_events(database_path)
    with sqlite3.connect(database_path) as connection:
        connection.execute(f"CREATE TABLE {_ROUND_SCOPED_EVENT_INDEX} (wrong TEXT)")
        connection.commit()
    before = _logical_dump(database_path)

    assert main(["migrate", "--database", str(database_path)]) == 1

    shown = capsys.readouterr()
    assert _ROUND_SCOPED_EVENT_INDEX in shown.err
    assert "will not alter" in shown.err
    assert _logical_dump(database_path) == before
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT version FROM atelier_schema_versions"
        ).fetchone() == (35,)


_PARKED_CURRENT_ATTEMPTS_V37 = "agent_attempts_after_the_transcript"


def _revert_the_attempt_transcript_pointer(connection: sqlite3.Connection) -> None:
    """Restore the attempt table as every schema from V27 to V36 published it.

    The #666 hop added the transcript address, and no hop between V27 and V36
    moved this table at all, so one published record is the record for all of
    them: a fixture claiming any of those versions is rebuilt to it and then
    refused by that version's own pinned fingerprint if anything else drifted.
    """

    _rebuild_product_table(
        connection,
        agent_attempts,
        _PARKED_CURRENT_ATTEMPTS_V37,
        _AGENT_ATTEMPTS_TRIGGERS,
        SCHEMA_VERSION,
        V36_SCHEMA_HANDOFF.version,
        trigger_source=_V32_AGENT_ATTEMPT_TRIGGERS,
    )
    _revert_the_redemption_owner(connection)


def _create_exact_v36_store(database_path: Path) -> None:
    """A current store with no transcript address: the published V36 shape.

    V36 differs from the current schema in one nullable column and the CHECK
    that shapes it -- no key, no trigger, no other table -- and the pinned V36
    fingerprint refuses the fixture the moment anything else about it drifts.
    """

    engine = create_canonical_engine(database_path)
    initialize_schema(engine)
    engine.dispose()
    with sqlite3.connect(database_path) as connection:
        _restore_v39_configuration_tables(connection)
        _revert_the_attempt_transcript_pointer(connection)
        _revert_the_abandoned_intent_state(connection)
        connection.execute(
            "UPDATE atelier_schema_versions SET version = ?",
            (V36_SCHEMA_HANDOFF.version,),
        )
        connection.commit()
        _require_product_shape(connection, V36_SCHEMA_HANDOFF.version)


_ARMED_RUN = RunId("live/haelt-den-versuch")
_ARMED_NODE = "bauen"
_ARMED_REQUEST_HASH = AgentExecutionRequestHash(Sha256Hash.of(b"bauen/anfrage").value)
_ARMED_PROCESS_OWNER = "owner/one-live-call"
_ARMED_WATCHDOG_GENERATION = "watchdog/one-live-call"


def _armed_attempt_values(revision_hash: WorkflowRevisionHash) -> dict[str, object]:
    """One attempt armed for launch, with every column that state fills.

    Written from the contracts rather than driven through the store: the durable
    runtime that would write it binds this whole process, and a hop is proved by
    what the table holds afterwards, not by which writer put it there. Armed
    rather than merely prepared because a hop is only shown to carry a column by
    a row that had something in it.
    """

    node_execution_id = NodeExecutionId.for_node(_ARMED_RUN, revision_hash, _ARMED_NODE)
    return {
        "attempt_id": AgentAttemptId.for_execution(
            node_execution_id, _ARMED_REQUEST_HASH, AGENT_ATTEMPT_ORDINAL
        ).value,
        "node_execution_id": node_execution_id.value,
        "request_hash": _ARMED_REQUEST_HASH.value,
        "executor_operational_identity": "headless-print-stream-json/v2",
        "run_id": _ARMED_RUN.value,
        "workflow_revision_hash": revision_hash.value,
        "node_id": _ARMED_NODE,
        "attempt_ordinal": AGENT_ATTEMPT_ORDINAL,
        "state": "LAUNCH_ARMED",
        "state_version": 1,
        "process_phase": "LAUNCH_AUTHORIZED",
        "process_owner_id": _ARMED_PROCESS_OWNER,
        "watchdog_generation_id": _ARMED_WATCHDOG_GENERATION,
        "runner_evidence_acceptance_phase": "NONE",
    }


_RUNNER_BINDING: Mapping[str, object] = {
    "runner_manifest_id": Sha256Hash.of(b"runner/one-manifest").value,
    "runner_generation_id": "generation/one-runner",
    "runner_invocation_id": "invocation/one-runner",
    "process_phase": "NONE",
    "process_owner_id": None,
    "watchdog_generation_id": None,
}
"""What makes an armed attempt a runner's rather than this host's own process."""


def _write_armed_attempt(
    connection: sqlite3.Connection,
    binding: Mapping[str, object] = {},
    agent_binding_set_hash: str | None = None,
) -> None:
    """One started run standing at an armed attempt, written into an open store.

    Shared by every fixture that measures an attempt hop, because the evidence
    each of them needs is the same row; what differs between them is only which
    shape the store was reverted to before it was written, and `binding` -- which
    of the two carriers the armed attempt belongs to, since each reaches FAILED
    through a transition of its own.
    """

    revision = WorkflowRevision(b"name: bauhuette\n")
    values = {**_armed_attempt_values(revision.revision_hash), **binding}
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute(
        "INSERT INTO workflow_revisions (revision_hash, document) VALUES (?, ?)",
        (revision.revision_hash.value, revision.document),
    )
    connection.execute(
        "INSERT INTO runs (run_id, bootstrap_workflow_id, revision_hash, "
        "workflow_format_version, current_node_id, current_round_ordinal, "
        "state, state_version, last_event_sequence, terminal_hash, "
        "agent_binding_set_hash) VALUES (?, ?, ?, ?, ?, ?, ?, 1, 0, NULL, ?)",
        (
            _ARMED_RUN.value,
            f"bootstrap-{_ARMED_RUN.value}",
            revision.revision_hash.value,
            # A format-1 run carries no binding set and a format-2 run must; the
            # column and the version are one fact, so the caller asking for the
            # hash is the caller asking for the format that admits it.
            1 if agent_binding_set_hash is None else 2,
            _ARMED_NODE,
            FIRST_ROUND_ORDINAL,
            RunState.STARTED.value,
            agent_binding_set_hash,
        ),
    )
    connection.execute(
        f"INSERT INTO agent_attempts ({', '.join(values)}) "
        f"VALUES ({', '.join('?' for _ in values)})",
        tuple(values.values()),
    )


def _populate_v36_attempt(database_path: Path) -> None:
    """A store claiming the pre-transcript version, holding one armed attempt."""

    with sqlite3.connect(database_path) as connection:
        _restore_v39_configuration_tables(connection)
        _revert_the_attempt_transcript_pointer(connection)
        _write_armed_attempt(connection)
        _revert_the_abandoned_intent_state(connection)
        connection.execute(
            "UPDATE atelier_schema_versions SET version = ?",
            (V36_SCHEMA_HANDOFF.version,),
        )
        connection.commit()
        _require_product_shape(connection, V36_SCHEMA_HANDOFF.version)


def _create_populated_v36_store(database_path: Path) -> None:
    """A published V36 store with one armed attempt already in it."""

    engine = create_canonical_engine(database_path)
    initialize_schema(engine)
    engine.dispose()
    _populate_v36_attempt(database_path)


_TRANSCRIPT_POINTER_COLUMN = "transcript_artifact_hash"
_KEPT_TRANSCRIPT = AttemptTranscript.of([AssistantTurn("I read the file and stopped.")])


def _replacement_attempt_row(
    database_path: Path,
    transcript_artifact_hash: str | None,
    *,
    ended: bool = True,
) -> tuple[str, tuple[object, ...]]:
    """A second, ended attempt of the stored execution, naming a transcript.

    It is the stored row with a new ordinal, the identity that ordinal derives,
    and the ending a transcript belongs to -- so every other constraint the
    table states is satisfied by the values the store itself wrote, and what is
    under test is the one column this hop added rather than a hand-built row.
    """

    with sqlite3.connect(database_path) as connection:
        columns = tuple(
            str(record[1])
            for record in connection.execute("PRAGMA table_info(agent_attempts)")
        )
        stored = dict(
            zip(
                columns,
                connection.execute(
                    f"SELECT {', '.join(columns)} FROM agent_attempts"
                ).fetchone(),
                strict=True,
            )
        )
    stored["attempt_ordinal"] = REPLACEMENT_AGENT_ATTEMPT_ORDINAL
    stored["attempt_id"] = AgentAttemptId.for_execution(
        NodeExecutionId(str(stored["node_execution_id"])),
        AgentExecutionRequestHash(str(stored["request_hash"])),
        REPLACEMENT_AGENT_ATTEMPT_ORDINAL,
    ).value
    # A transcript is what an attempt DID, so the row that may carry one is an
    # ended one. The store writes both in the same statement; this fixture
    # states the same ending by hand because it writes no attempt through it.
    if ended:
        stored["state"] = AgentAttemptState.FAILED.value
        stored["state_version"] = 2
        stored["failure_code"] = (
            AgentAttemptFailureCode.PROCESS_EXITED_UNSUCCESSFULLY.value
        )
    if _TRANSCRIPT_POINTER_COLUMN in stored:
        stored[_TRANSCRIPT_POINTER_COLUMN] = transcript_artifact_hash
    statement = (
        f"INSERT INTO agent_attempts ({', '.join(stored)}) "
        f"VALUES ({', '.join('?' for _ in stored)})"
    )
    return statement, tuple(stored.values())


def test_an_exact_v36_store_migrates_to_v37_by_adding_the_transcript_pointer(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database_path = tmp_path / "atelier.sqlite"
    _create_exact_v36_store(database_path)

    engine = create_canonical_engine(database_path)
    with pytest.raises(MigrationRequired, match="schema version 36"):
        initialize_schema(engine)
    engine.dispose()

    assert main(["migrate", "--database", str(database_path)]) == 0
    shown = capsys.readouterr()
    assert "36" in shown.out and "37" in shown.out
    assert PRODUCT_SCHEMA_HANDOFF.fingerprint_sha256 in shown.out

    engine = create_canonical_engine(database_path)
    initialize_schema(engine)
    with engine.connect() as connection:
        assert (
            connection.scalar(sa.select(atelier_schema_versions.c.version))
            == SCHEMA_VERSION
        )
        assert tuple(agent_attempts.c.keys())[-1] == _TRANSCRIPT_POINTER_COLUMN
    engine.dispose()


def test_every_v36_attempt_column_crosses_the_v37_rebuild_unchanged(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The hop adds a column; it reinterprets none, and invents no address.

    A predecessor attempt carries no transcript, and what that must mean after
    the hop is NULL -- never a pointer at bytes nobody kept.
    """

    database_path = tmp_path / "atelier.sqlite"
    _create_populated_v36_store(database_path)
    with sqlite3.connect(database_path) as connection:
        predecessor_columns = tuple(
            str(record[1])
            for record in connection.execute("PRAGMA table_info(agent_attempts)")
        )
        predecessor_row = connection.execute(
            f"SELECT {', '.join(predecessor_columns)} FROM agent_attempts"
        ).fetchone()
    assert predecessor_row is not None
    assert _TRANSCRIPT_POINTER_COLUMN not in predecessor_columns

    assert main(["migrate", "--database", str(database_path)]) == 0
    capsys.readouterr()

    with sqlite3.connect(database_path) as connection:
        assert (
            connection.execute(
                f"SELECT {', '.join(predecessor_columns)} FROM agent_attempts"
            ).fetchone()
            == predecessor_row
        )
        assert connection.execute(
            f"SELECT {_TRANSCRIPT_POINTER_COLUMN} FROM agent_attempts"
        ).fetchone() == (None,)


def test_a_v37_store_admits_the_transcript_pointer_its_predecessor_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The one sentence the hop exists for, asked of both sides of it."""

    database_path = tmp_path / "atelier.sqlite"
    _create_populated_v36_store(database_path)
    kept = Artifact(_KEPT_TRANSCRIPT.document)

    statement, row = _replacement_attempt_row(database_path, None)
    with (
        sqlite3.connect(database_path) as connection,
        pytest.raises(sqlite3.OperationalError, match=_TRANSCRIPT_POINTER_COLUMN),
    ):
        connection.execute(
            statement.replace(
                "attempt_id,", f"{_TRANSCRIPT_POINTER_COLUMN}, attempt_id,", 1
            ).replace("VALUES (?,", "VALUES (?, ?,", 1),
            (kept.artifact_hash.value, *row),
        )

    assert main(["migrate", "--database", str(database_path)]) == 0
    capsys.readouterr()

    statement, row = _replacement_attempt_row(database_path, kept.artifact_hash.value)
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "INSERT INTO artifacts (artifact_hash, content) VALUES (?, ?)",
            (kept.artifact_hash.value, kept.content),
        )
        connection.execute(statement, row)
        connection.commit()
        assert connection.execute(
            f"SELECT {_TRANSCRIPT_POINTER_COLUMN} FROM agent_attempts "
            "WHERE attempt_ordinal = ?",
            (REPLACEMENT_AGENT_ATTEMPT_ORDINAL,),
        ).fetchone() == (kept.artifact_hash.value,)


def test_a_v37_attempt_refuses_a_pointer_that_is_no_content_address(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A pointer no artifact could ever answer is refused where it is written.

    The column is free to be empty -- most attempts have no transcript -- so the
    guard that still means something is its shape: sixty-four hex characters, or
    nothing.
    """

    database_path = tmp_path / "atelier.sqlite"
    _create_populated_v36_store(database_path)
    assert main(["migrate", "--database", str(database_path)]) == 0
    capsys.readouterr()
    statement, row = _replacement_attempt_row(database_path, "not-a-content-address")

    with (
        sqlite3.connect(database_path) as connection,
        pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"),
    ):
        connection.execute(statement, row)


def test_the_attempt_ledger_is_guarded_again_after_the_v37_rebuild(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The rebuild puts both attempt triggers back, not only the table.

    An attempt row that could be deleted, or could take a transition its own
    ledger forbids, would be no ledger for the length of the hop and afterwards.
    """

    database_path = tmp_path / "atelier.sqlite"
    _create_populated_v36_store(database_path)
    assert main(["migrate", "--database", str(database_path)]) == 0
    capsys.readouterr()

    with sqlite3.connect(database_path) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("DELETE FROM agent_attempts")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("UPDATE agent_attempts SET state = 'SUCCEEDED'")


@pytest.mark.parametrize(
    "collision_sql",
    [
        pytest.param(
            f"CREATE TABLE {_PREDECESSOR_ATTEMPTS_BEFORE_THE_TRANSCRIPT}(wrong TEXT)",
            id="table",
        ),
        pytest.param(
            f"CREATE VIEW {_PREDECESSOR_ATTEMPTS_BEFORE_THE_TRANSCRIPT} "
            "AS SELECT 1 AS wrong",
            id="view",
        ),
    ],
)
def test_a_refused_transcript_hop_leaves_the_v36_store_untouched(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], collision_sql: str
) -> None:
    """A foreign object under the parking name refuses before the first statement."""

    database_path = tmp_path / "atelier.sqlite"
    _create_exact_v36_store(database_path)
    with sqlite3.connect(database_path) as connection:
        connection.execute(collision_sql)
        connection.commit()
    before = _logical_dump(database_path)

    assert main(["migrate", "--database", str(database_path)]) == 1

    shown = capsys.readouterr()
    assert _PREDECESSOR_ATTEMPTS_BEFORE_THE_TRANSCRIPT in shown.err
    assert "will not alter" in shown.err
    assert _logical_dump(database_path) == before
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT version FROM atelier_schema_versions"
        ).fetchone() == (36,)


def test_a_v37_attempt_refuses_a_transcript_no_artifact_answers(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A well-formed address is not evidence; bytes under it are.

    Sixty-four hex characters look exactly like a kept transcript whether or not
    anything was ever kept, so the shape alone would let a dangling pointer pass
    for the one thing an operator opens this column to read.
    """

    database_path = tmp_path / "atelier.sqlite"
    _create_populated_v36_store(database_path)
    assert main(["migrate", "--database", str(database_path)]) == 0
    capsys.readouterr()
    unpublished = Artifact(b'{"kind":"attempt-transcript/v1","events":[]}')
    statement, row = _replacement_attempt_row(
        database_path, unpublished.artifact_hash.value
    )

    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
            connection.execute(statement, row)


def test_a_transcript_a_v37_attempt_named_can_never_be_moved_or_cleared(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The address goes from absent to present once and then stands.

    Every other column of a terminal attempt is already fenced by the transition
    trigger. Without this the pointer would be the single field of a finished
    attempt that a later update could still swing at other bytes -- or blank,
    which reads back as "this attempt decoded nothing".
    """

    database_path = tmp_path / "atelier.sqlite"
    _create_populated_v36_store(database_path)
    assert main(["migrate", "--database", str(database_path)]) == 0
    capsys.readouterr()
    kept = Artifact(_KEPT_TRANSCRIPT.document)
    other = Artifact(
        AttemptTranscript.of([AssistantTurn("Another attempt entirely.")]).document
    )
    statement, row = _replacement_attempt_row(database_path, kept.artifact_hash.value)

    with sqlite3.connect(database_path) as connection:
        for artifact in (kept, other):
            connection.execute(
                "INSERT INTO artifacts (artifact_hash, content) VALUES (?, ?)",
                (artifact.artifact_hash.value, artifact.content),
            )
        connection.execute(statement, row)
        connection.commit()

        for swung in (other.artifact_hash.value, None):
            with pytest.raises(sqlite3.IntegrityError, match="invalid agent attempt"):
                connection.execute(
                    f"UPDATE agent_attempts SET {_TRANSCRIPT_POINTER_COLUMN} = ?, "
                    "state_version = state_version + 1 "
                    "WHERE attempt_ordinal = ?",
                    (swung, REPLACEMENT_AGENT_ATTEMPT_ORDINAL),
                )


def test_a_v37_attempt_still_running_may_not_name_a_transcript_yet(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A transcript is what an attempt did, so a live one has none to name.

    Written as an insert because that is the door the CHECK is the only guard
    on: an armed row is fenced by the transition trigger against every update,
    this column included, but nothing before this said a freshly written row
    could not claim a finished account of work still going on.
    """

    database_path = tmp_path / "atelier.sqlite"
    _create_populated_v36_store(database_path)
    assert main(["migrate", "--database", str(database_path)]) == 0
    capsys.readouterr()
    kept = Artifact(_KEPT_TRANSCRIPT.document)
    statement, row = _replacement_attempt_row(
        database_path, kept.artifact_hash.value, ended=False
    )

    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "INSERT INTO artifacts (artifact_hash, content) VALUES (?, ?)",
            (kept.artifact_hash.value, kept.content),
        )
        with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
            connection.execute(statement, row)


_PARKED_CURRENT_INTENTS_V38 = "effect_intents_after_abandonment"


def _revert_the_abandoned_intent_state(connection: sqlite3.Connection) -> None:
    """Restore the intent table as every schema up to V37 published it.

    The #705 hop widened one CHECK and added the two triggers guarding the word
    it admits; no earlier hop moved this table at all, so one published record
    is the record for every version before V38, and each fixture's own pinned
    fingerprint refuses it the moment anything else about it drifted.
    """

    for trigger in _EFFECT_INTENTS_ABANDONMENT_TRIGGERS:
        connection.execute(f"DROP TRIGGER {trigger}")
    _rebuild_product_table(
        connection,
        effect_intents,
        _PARKED_CURRENT_INTENTS_V38,
        _EFFECT_INTENTS_TRIGGERS,
        SCHEMA_VERSION,
        V37_SCHEMA_HANDOFF.version,
        trigger_source=_V41_EFFECT_INTENT_TRIGGERS,
    )


_PARKED_CURRENT_ATTEMPTS_V39 = "agent_attempts_after_the_candidate_capture"
_PARKED_CURRENT_REDEMPTIONS_V39 = "tool_redemptions_owned_by_the_attempt"


def _revert_the_redemption_owner(connection: sqlite3.Connection) -> None:
    """Hang a redemption back off the agent receipt, as V15 to V38 published it.

    Rebuilt to V38's record whichever of those versions the fixture claims,
    because no hop between them moved this table: the text is one text, and the
    version each fixture then declares is refused by its own pinned fingerprint
    if anything else about the store drifted.

    Asked by every revert that takes a store back past V39, and it answers only
    once. Which of them a fixture calls depends on how far back it goes, and
    several call more than one; making the step a no-op when the table already
    stands in its published shape keeps each of those reverts able to say what
    it needs without any of them having to know what the others did.
    """

    if _published_redemption_shape_stands(connection):
        return
    _rebuild_product_table(
        connection,
        tool_redemptions,
        _PARKED_CURRENT_REDEMPTIONS_V39,
        _TOOL_REDEMPTIONS_TRIGGERS,
        SCHEMA_VERSION,
        V38_SCHEMA_HANDOFF.version,
    )


def _published_redemption_shape_stands(connection: sqlite3.Connection) -> bool:
    """Whether this store already holds the redemption table V38 published."""

    standing = connection.execute(
        "SELECT sql FROM sqlite_master WHERE name = ?", (tool_redemptions.name,)
    ).fetchone()
    published = PUBLISHED_TABLE_SHAPES[
        (V38_SCHEMA_HANDOFF.version, tool_redemptions.name)
    ]
    return standing is not None and str(standing[0]).strip() == published.strip()


def _revert_the_candidate_capture_code(connection: sqlite3.Connection) -> None:
    """Restore both tables the #642 hop moved, as V37 and V38 published them.

    The hop widened one CHECK and both FAILED transitions of the attempt table,
    and re-owned a redemption from the success-only agent receipt to the attempt
    itself. No hop between V37 and V38 moved either table, so one published
    record is the record for both versions, and each fixture's own pinned
    fingerprint refuses it the moment anything else about them drifted.
    """

    _rebuild_product_table(
        connection,
        agent_attempts,
        _PARKED_CURRENT_ATTEMPTS_V39,
        _AGENT_ATTEMPTS_TRIGGERS,
        SCHEMA_VERSION,
        V38_SCHEMA_HANDOFF.version,
        trigger_source=_V38_AGENT_ATTEMPT_TRIGGERS,
    )
    _revert_the_redemption_owner(connection)


def _create_exact_v37_store(database_path: Path) -> None:
    """A current store with no word for an abandoned intent: the V37 shape.

    V37 differs from the current schema in one CHECK and two triggers -- no
    column, no key, no other table -- and the pinned V37 fingerprint refuses
    the fixture the moment anything else about it drifts.
    """

    engine = create_canonical_engine(database_path)
    initialize_schema(engine)
    engine.dispose()
    with sqlite3.connect(database_path) as connection:
        _restore_v39_configuration_tables(connection)
        _revert_the_candidate_capture_code(connection)
        _revert_the_abandoned_intent_state(connection)
        connection.execute(
            "UPDATE atelier_schema_versions SET version = ?",
            (V37_SCHEMA_HANDOFF.version,),
        )
        connection.commit()
        _require_product_shape(connection, V37_SCHEMA_HANDOFF.version)


_DRIVERLESS_RUN = RunId("live/wirken-ohne-antwort")
_DRIVERLESS_ACTION_NODE = "wirken"
_DRIVERLESS_INTENT_KEY = LogicalEffectKey("wirken/ohne-antwort")
_DRIVERLESS_REQUEST = b"wirken/anfrage-ohne-antwort"
_DRIVERLESS_REVISION = WorkflowRevision(b"name: wirkstatt\n")
"""The one document the run below is bound to, so a test asking what the
hop carried names the same revision the fixture wrote."""


def _prepared_intent_values() -> dict[str, object]:
    """One intent prepared and never resolved, with every column that fills.

    Written from the contracts rather than driven through the store: what a hop
    carries is proved by what the table holds afterwards, not by which writer
    put it there.
    """

    return {
        "logical_key": _DRIVERLESS_INTENT_KEY.value,
        "run_id": _DRIVERLESS_RUN.value,
        "canonical_request": _DRIVERLESS_REQUEST,
        "request_hash": Sha256Hash.of(_DRIVERLESS_REQUEST).value,
        "workflow_revision_hash": _DRIVERLESS_REVISION.revision_hash.value,
        "adapter_revision": "loopback-v1",
        "destination_identity": "loopback-test",
        "adapter_operational_identity": "operational/loopback",
        "state": EffectIntentState.PREPARED.value,
        "state_version": EFFECT_INTENT_VERSION_INITIAL.value,
    }


def _create_populated_v37_store(database_path: Path) -> None:
    """A published V37 store holding one prepared intent on an ended run.

    The run stands FAILED at the Action node its intent was prepared on, which
    is exactly the shape #705 found no word for: nothing will resolve the
    intent, and nothing can lift the run to the operator's door either.
    """

    engine = create_canonical_engine(database_path)
    initialize_schema(engine)
    engine.dispose()
    revision = _DRIVERLESS_REVISION
    values = _prepared_intent_values()
    with sqlite3.connect(database_path) as connection:
        _restore_v39_configuration_tables(connection)
        _revert_the_candidate_capture_code(connection)
        _revert_the_abandoned_intent_state(connection)
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(
            "INSERT INTO workflow_revisions (revision_hash, document) VALUES (?, ?)",
            (revision.revision_hash.value, revision.document),
        )
        connection.execute(
            "INSERT INTO runs (run_id, bootstrap_workflow_id, revision_hash, "
            "workflow_format_version, current_node_id, current_round_ordinal, "
            "state, state_version, last_event_sequence, terminal_hash) "
            "VALUES (?, ?, ?, 1, ?, ?, ?, 2, 1, ?)",
            (
                _DRIVERLESS_RUN.value,
                f"bootstrap-{_DRIVERLESS_RUN.value}",
                revision.revision_hash.value,
                _DRIVERLESS_ACTION_NODE,
                FIRST_ROUND_ORDINAL,
                RunState.FAILED.value,
                Sha256Hash.of(b"terminal/wirkstatt").value,
            ),
        )
        connection.execute(
            f"INSERT INTO effect_intents ({', '.join(values)}) "
            f"VALUES ({', '.join('?' for _ in values)})",
            tuple(values.values()),
        )
        connection.execute(
            "UPDATE atelier_schema_versions SET version = ?",
            (V37_SCHEMA_HANDOFF.version,),
        )
        connection.commit()
        _require_product_shape(connection, V37_SCHEMA_HANDOFF.version)


_ABANDON_THE_PREPARED_INTENT = (
    "UPDATE effect_intents SET state = ?, state_version = ? WHERE logical_key = ?"
)
_ABANDONED_ROW = (
    EffectIntentState.ABANDONED.value,
    EFFECT_INTENT_VERSION_ABANDONED.value,
    _DRIVERLESS_INTENT_KEY.value,
)


def test_an_exact_v37_store_migrates_to_v38_by_admitting_an_abandoned_intent(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database_path = tmp_path / "atelier.sqlite"
    _create_exact_v37_store(database_path)

    engine = create_canonical_engine(database_path)
    with pytest.raises(MigrationRequired, match="schema version 37"):
        initialize_schema(engine)
    engine.dispose()

    assert main(["migrate", "--database", str(database_path)]) == 0
    shown = capsys.readouterr()
    assert "37" in shown.out and "38" in shown.out
    assert PRODUCT_SCHEMA_HANDOFF.fingerprint_sha256 in shown.out

    engine = create_canonical_engine(database_path)
    initialize_schema(engine)
    with engine.connect() as connection:
        assert (
            connection.scalar(sa.select(atelier_schema_versions.c.version))
            == SCHEMA_VERSION
        )
    engine.dispose()


def test_every_v37_intent_column_crosses_the_v38_rebuild_unchanged(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The hop widens a vocabulary; it reinterprets no stored intent.

    A prepared intent this store already holds is still prepared afterwards.
    Deciding that one is abandoned is the serve-start sweep's word, taken on
    evidence about its driver, and never something a hop may say for it.
    """

    database_path = tmp_path / "atelier.sqlite"
    _create_populated_v37_store(database_path)
    columns = tuple(_prepared_intent_values())
    projected = ", ".join(columns)
    with sqlite3.connect(database_path) as connection:
        predecessor_row = connection.execute(
            f"SELECT {projected} FROM effect_intents"
        ).fetchone()
    assert predecessor_row is not None

    assert main(["migrate", "--database", str(database_path)]) == 0
    capsys.readouterr()

    with sqlite3.connect(database_path) as connection:
        assert (
            connection.execute(f"SELECT {projected} FROM effect_intents").fetchone()
            == predecessor_row
        )


def test_a_v38_intent_admits_the_abandonment_its_predecessor_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The one sentence the hop exists for, asked of both sides of it."""

    database_path = tmp_path / "atelier.sqlite"
    _create_populated_v37_store(database_path)

    with (
        sqlite3.connect(database_path) as connection,
        pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"),
    ):
        connection.execute(_ABANDON_THE_PREPARED_INTENT, _ABANDONED_ROW)

    assert main(["migrate", "--database", str(database_path)]) == 0
    capsys.readouterr()

    with sqlite3.connect(database_path) as connection:
        connection.execute(_ABANDON_THE_PREPARED_INTENT, _ABANDONED_ROW)
        connection.commit()
        assert connection.execute(
            "SELECT state, state_version FROM effect_intents"
        ).fetchone() == (
            EffectIntentState.ABANDONED.value,
            EFFECT_INTENT_VERSION_ABANDONED.value,
        )


@pytest.mark.parametrize(
    ("prior", "attempted"),
    [
        pytest.param(
            None,
            (
                EffectIntentState.ABANDONED.value,
                EFFECT_INTENT_VERSION_WAITING.value + 1,
                _DRIVERLESS_INTENT_KEY.value,
            ),
            id="abandonment-at-a-version-no-prepared-intent-advances-to",
        ),
        pytest.param(
            (
                EffectIntentState.CONFIRMED.value,
                EFFECT_INTENT_VERSION_CONFIRMED_INITIAL.value,
                _DRIVERLESS_INTENT_KEY.value,
            ),
            _ABANDONED_ROW,
            id="abandonment-of-an-intent-that-already-has-its-receipt",
        ),
        pytest.param(
            _ABANDONED_ROW,
            (
                EffectIntentState.PREPARED.value,
                EFFECT_INTENT_VERSION_INITIAL.value,
                _DRIVERLESS_INTENT_KEY.value,
            ),
            id="revival-of-an-intent-its-run-already-ended-without",
        ),
    ],
)
def test_a_v38_intent_reaches_abandoned_only_from_prepared_and_never_leaves(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    prior: tuple[str, int, str] | None,
    attempted: tuple[str, int, str],
) -> None:
    """The word is terminal, and it means one thing: this run ended without it.

    The CHECK admits the vocabulary; only the trigger says which writes may
    use it. Without it an abandonment could be written over a confirmed
    receipt, or taken back the next time something touched the row -- both of
    them a durable lie about an effect the destination may well have performed.
    """

    database_path = tmp_path / "atelier.sqlite"
    _create_populated_v37_store(database_path)
    assert main(["migrate", "--database", str(database_path)]) == 0
    capsys.readouterr()

    with sqlite3.connect(database_path) as connection:
        if prior is not None:
            connection.execute(_ABANDON_THE_PREPARED_INTENT, prior)
            connection.commit()
        with pytest.raises(
            sqlite3.IntegrityError, match="invalid effect intent abandonment"
        ):
            connection.execute(_ABANDON_THE_PREPARED_INTENT, attempted)


def test_a_v38_intent_is_never_written_abandoned_in_the_first_place(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The transition trigger guards one door; an insert is the other.

    ABANDONED means "the run this intent was prepared on ended without
    resolving it", and every word in that sentence is about a row that already
    existed. A freshly written one has no run behind it that ended and no
    prepared request anyone ever meant to send, so an intent born abandoned
    would be an ending the store could not account for -- and the transition
    trigger, which only ever sees an UPDATE, would never notice.
    """

    database_path = tmp_path / "atelier.sqlite"
    _create_populated_v37_store(database_path)
    assert main(["migrate", "--database", str(database_path)]) == 0
    capsys.readouterr()
    values = _prepared_intent_values() | {
        "logical_key": "wirken/nie-vorbereitet",
        "operation_name": AdapterOperationName.OPEN_PR.value,
        "state": EffectIntentState.ABANDONED.value,
        "state_version": EFFECT_INTENT_VERSION_ABANDONED.value,
    }

    statement = (
        f"INSERT INTO effect_intents ({', '.join(values)}) "
        f"VALUES ({', '.join('?' for _ in values)})"
    )

    with (
        sqlite3.connect(database_path) as connection,
        pytest.raises(
            sqlite3.IntegrityError, match="effect intents are not born abandoned"
        ),
    ):
        connection.execute(statement, tuple(values.values()))

    # The same row, born the one way an intent is born, still lands: what the
    # trigger refuses is the word, not the write.
    born = values | {
        "state": EffectIntentState.PREPARED.value,
        "state_version": EFFECT_INTENT_VERSION_INITIAL.value,
    }
    with sqlite3.connect(database_path) as connection:
        connection.execute(statement, tuple(born.values()))
        connection.commit()
        assert connection.execute(
            "SELECT state FROM effect_intents WHERE logical_key = ?",
            (born["logical_key"],),
        ).fetchone() == (EffectIntentState.PREPARED.value,)


def test_the_intent_ledger_is_guarded_again_after_the_v38_rebuild(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The rebuild puts both predecessor triggers back, not only the table.

    An intent row that could be deleted, or could have the request bytes it
    was prepared with rewritten, would be no ledger at all -- for the length of
    the hop and afterwards.
    """

    database_path = tmp_path / "atelier.sqlite"
    _create_populated_v37_store(database_path)
    assert main(["migrate", "--database", str(database_path)]) == 0
    capsys.readouterr()

    with sqlite3.connect(database_path) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("DELETE FROM effect_intents")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE effect_intents SET canonical_request = ?", (b"etwas anderes",)
            )


@pytest.mark.parametrize(
    "collision_sql",
    [
        pytest.param(
            f"CREATE TABLE {_PREDECESSOR_INTENTS_BEFORE_ABANDONMENT}(wrong TEXT)",
            id="table",
        ),
        pytest.param(
            f"CREATE VIEW {_PREDECESSOR_INTENTS_BEFORE_ABANDONMENT} "
            "AS SELECT 1 AS wrong",
            id="view",
        ),
    ],
)
def test_a_refused_abandonment_hop_leaves_the_v37_store_untouched(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], collision_sql: str
) -> None:
    """A foreign object under the parking name refuses before the first statement."""

    database_path = tmp_path / "atelier.sqlite"
    _create_exact_v37_store(database_path)
    with sqlite3.connect(database_path) as connection:
        connection.execute(collision_sql)
        connection.commit()
    before = _logical_dump(database_path)

    assert main(["migrate", "--database", str(database_path)]) == 1

    shown = capsys.readouterr()
    assert "will not alter" in shown.err
    assert _logical_dump(database_path) == before
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT version FROM atelier_schema_versions"
        ).fetchone() == (37,)


def _create_exact_v38_store(database_path: Path) -> None:
    """A current store with no word for work that was done and lost: the V38 shape.

    V38 differs from the current schema in one CHECK and one transition -- no
    column, no key, no other table -- and the pinned V38 fingerprint refuses the
    fixture the moment anything else about it drifts.
    """

    engine = create_canonical_engine(database_path)
    initialize_schema(engine)
    engine.dispose()
    with sqlite3.connect(database_path) as connection:
        _restore_v39_configuration_tables(connection)
        _revert_the_candidate_capture_code(connection)
        connection.execute(
            "UPDATE atelier_schema_versions SET version = ?",
            (V38_SCHEMA_HANDOFF.version,),
        )
        connection.commit()
        _require_product_shape(connection, V38_SCHEMA_HANDOFF.version)


def _create_populated_v38_store(database_path: Path) -> None:
    """A published V38 store with one armed attempt already in it."""

    engine = create_canonical_engine(database_path)
    initialize_schema(engine)
    engine.dispose()
    with sqlite3.connect(database_path) as connection:
        _restore_v39_configuration_tables(connection)
        _revert_the_candidate_capture_code(connection)
        _write_armed_attempt(connection)
        connection.execute(
            "UPDATE atelier_schema_versions SET version = ?",
            (V38_SCHEMA_HANDOFF.version,),
        )
        connection.commit()
        _require_product_shape(connection, V38_SCHEMA_HANDOFF.version)


def _create_populated_v39_store(database_path: Path) -> None:
    engine = create_canonical_engine(database_path)
    initialize_schema(engine)
    engine.dispose()
    with sqlite3.connect(database_path) as connection:
        _restore_v39_configuration_tables(connection)
        connection.execute(
            "INSERT INTO host_occupancy_revisions VALUES (?, ?, ?, ?)",
            ("ab" * 32, "studio", "cd" * 32, 1),
        )
        connection.execute(
            "INSERT INTO host_occupancy_bindings VALUES (?, ?, ?)",
            ("ab" * 32, "builder", "ef" * 32),
        )
        connection.execute(
            "UPDATE atelier_schema_versions SET version = ?",
            (V39_SCHEMA_HANDOFF.version,),
        )
        connection.commit()
        _require_product_shape(connection, V39_SCHEMA_HANDOFF.version)


def test_v40_retires_populated_lineage_occupancy_without_inventing_defaults(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database_path = tmp_path / "atelier.sqlite"
    _create_populated_v39_store(database_path)

    assert main(["migrate", "--database", str(database_path)]) == 0
    shown = capsys.readouterr()
    assert "39" in shown.out and "40" in shown.out

    with sqlite3.connect(database_path) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert "host_occupancy_revisions" not in tables
        assert "host_occupancy_bindings" not in tables
        assert {
            "host_model_registry_revisions",
            "host_model_registry_entries",
            "host_project_model_defaults_revisions",
            "host_project_model_defaults",
        } <= tables
        assert connection.execute(
            "SELECT count(*) FROM host_project_model_defaults"
        ).fetchone() == (0,)
        _require_product_shape(connection, SCHEMA_VERSION)


@pytest.mark.parametrize(
    ("collision_sql", "drop_sql", "name"),
    (
        pytest.param(
            ("CREATE TABLE host_model_registry_revisions(wrong TEXT)",),
            ("DROP TABLE host_model_registry_revisions",),
            "host_model_registry_revisions",
            id="replacement-table",
        ),
        pytest.param(
            ("CREATE VIEW host_model_registry_entries AS SELECT 1 AS wrong",),
            ("DROP VIEW host_model_registry_entries",),
            "host_model_registry_entries",
            id="replacement-view",
        ),
        pytest.param(
            (
                "CREATE TABLE replacement_trigger_target(wrong TEXT)",
                (
                    "CREATE TRIGGER host_project_model_defaults_no_update "
                    "AFTER INSERT ON replacement_trigger_target BEGIN SELECT 1; END"
                ),
            ),
            (
                "DROP TRIGGER host_project_model_defaults_no_update",
                "DROP TABLE replacement_trigger_target",
            ),
            "host_project_model_defaults_no_update",
            id="replacement-trigger",
        ),
    ),
)
def test_v40_collision_keeps_occupancy_and_version_until_a_clean_retry(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    collision_sql: tuple[str, ...],
    drop_sql: tuple[str, ...],
    name: str,
) -> None:
    """The replacement preflight refuses before it retires V39 occupancy."""

    database_path = tmp_path / "atelier.sqlite"
    _create_populated_v39_store(database_path)
    with sqlite3.connect(database_path) as connection:
        occupancy_before = (
            connection.execute("SELECT * FROM host_occupancy_revisions").fetchall(),
            connection.execute("SELECT * FROM host_occupancy_bindings").fetchall(),
        )
        for statement in collision_sql:
            connection.execute(statement)
        connection.commit()

    assert main(["migrate", "--database", str(database_path)]) == 1
    shown = capsys.readouterr()
    assert name in shown.err
    assert "will not alter" in shown.err
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT version FROM atelier_schema_versions"
        ).fetchone() == (V39_SCHEMA_HANDOFF.version,)
        assert (
            connection.execute("SELECT * FROM host_occupancy_revisions").fetchall(),
            connection.execute("SELECT * FROM host_occupancy_bindings").fetchall(),
        ) == occupancy_before
        for statement in drop_sql:
            connection.execute(statement)
        connection.commit()

    assert main(["migrate", "--database", str(database_path)]) == 0
    capsys.readouterr()
    migrated = _logical_dump(database_path)
    assert main(["migrate", "--database", str(database_path)]) == 0
    assert _logical_dump(database_path) == migrated
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT version FROM atelier_schema_versions"
        ).fetchone() == (SCHEMA_VERSION,)
        assert connection.execute(
            "SELECT count(*) FROM host_project_model_defaults"
        ).fetchone() == (0,)


def test_an_exact_v38_store_migrates_to_v39_by_admitting_a_lost_candidate(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database_path = tmp_path / "atelier.sqlite"
    _create_exact_v38_store(database_path)

    engine = create_canonical_engine(database_path)
    with pytest.raises(MigrationRequired, match="schema version 38"):
        initialize_schema(engine)
    engine.dispose()

    assert main(["migrate", "--database", str(database_path)]) == 0
    shown = capsys.readouterr()
    assert "38" in shown.out and "39" in shown.out
    assert PRODUCT_SCHEMA_HANDOFF.fingerprint_sha256 in shown.out

    engine = create_canonical_engine(database_path)
    initialize_schema(engine)
    with engine.connect() as connection:
        assert (
            connection.scalar(sa.select(atelier_schema_versions.c.version))
            == SCHEMA_VERSION
        )
    engine.dispose()


def test_every_v38_attempt_column_crosses_the_v39_rebuild_unchanged(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The hop widens a vocabulary; it reinterprets no stored attempt.

    An attempt this store already holds could not have failed for a reason the
    schema had no word for, so the hop backfills nothing and rewrites nothing --
    it only makes the next attempt able to say it.
    """

    database_path = tmp_path / "atelier.sqlite"
    _create_populated_v38_store(database_path)
    columns = tuple(
        _armed_attempt_values(WorkflowRevision(b"name: bauhuette\n").revision_hash)
    )
    projected = ", ".join(columns)
    with sqlite3.connect(database_path) as connection:
        predecessor_row = connection.execute(
            f"SELECT {projected} FROM agent_attempts"
        ).fetchone()
    assert predecessor_row is not None

    assert main(["migrate", "--database", str(database_path)]) == 0
    capsys.readouterr()

    with sqlite3.connect(database_path) as connection:
        assert (
            connection.execute(f"SELECT {projected} FROM agent_attempts").fetchone()
            == predecessor_row
        )


_FAIL_THE_LOCAL_ATTEMPT = (
    "UPDATE agent_attempts SET state = 'FAILED', state_version = 2, failure_code = ?"
)
_FAIL_THE_RUNNER_ATTEMPT = (
    "UPDATE agent_attempts SET state = 'FAILED', state_version = 2, "
    "failure_code = ?, runner_terminal_evidence_hash = ?, "
    "runner_evidence_acceptance_phase = 'CORE_COMMITTED'"
)
_RUNNER_EVIDENCE_HASH = Sha256Hash.of(b"runner/terminal-evidence").value


def _populated_v38_store_with(
    database_path: Path, binding: Mapping[str, object]
) -> None:
    """A published V38 store holding one armed attempt of the named carrier."""

    engine = create_canonical_engine(database_path)
    initialize_schema(engine)
    engine.dispose()
    with sqlite3.connect(database_path) as connection:
        _restore_v39_configuration_tables(connection)
        _revert_the_candidate_capture_code(connection)
        _write_armed_attempt(connection, binding)
        connection.execute(
            "UPDATE atelier_schema_versions SET version = ?",
            (V38_SCHEMA_HANDOFF.version,),
        )
        connection.commit()
        _require_product_shape(connection, V38_SCHEMA_HANDOFF.version)


@pytest.mark.parametrize(
    ("binding", "statement", "arguments"),
    [
        pytest.param(
            {},
            _FAIL_THE_LOCAL_ATTEMPT,
            (AgentAttemptFailureCode.CANDIDATE_CAPTURE_FAILED.value,),
            id="the-attempt-this-host-ran-itself",
        ),
        pytest.param(
            _RUNNER_BINDING,
            _FAIL_THE_RUNNER_ATTEMPT,
            (
                AgentAttemptFailureCode.CANDIDATE_CAPTURE_FAILED.value,
                _RUNNER_EVIDENCE_HASH,
            ),
            id="the-attempt-a-runner-returned-evidence-for",
        ),
    ],
)
def test_every_failed_transition_admits_the_lost_candidate_only_after_the_v39_hop(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    binding: Mapping[str, object],
    statement: str,
    arguments: tuple[str, ...],
) -> None:
    """One vocabulary, so both ways an attempt ends FAILED gain the word together.

    An attempt reaches FAILED through the transition its carrier owns, and a
    schema admitting a code on one of them but not the other would hold two
    answers to "which failure codes exist" -- the CHECK's and the trigger's. The
    hop is asked from both sides here for that reason, not because both carriers
    capture a candidate today.
    """

    database_path = tmp_path / "atelier.sqlite"
    _populated_v38_store_with(database_path, binding)

    with (
        sqlite3.connect(database_path) as connection,
        pytest.raises(sqlite3.IntegrityError),
    ):
        connection.execute(statement, arguments)

    assert main(["migrate", "--database", str(database_path)]) == 0
    capsys.readouterr()

    with sqlite3.connect(database_path) as connection:
        connection.execute(statement, arguments)
        connection.commit()
        assert connection.execute(
            "SELECT state, failure_code FROM agent_attempts"
        ).fetchone() == (
            "FAILED",
            AgentAttemptFailureCode.CANDIDATE_CAPTURE_FAILED.value,
        )


def _populated_v49_store_with(
    database_path: Path, binding: Mapping[str, object]
) -> None:
    """A published V49 store holding one armed attempt of the named carrier."""

    engine = create_canonical_engine(database_path)
    initialize_schema(engine)
    engine.dispose()
    with sqlite3.connect(database_path) as connection:
        _restore_v49_attempt_failure_vocabulary(connection)
        _write_armed_attempt(connection, binding)
        connection.execute(
            "UPDATE atelier_schema_versions SET version = ?",
            (V49_SCHEMA_HANDOFF.version,),
        )
        connection.commit()
        _require_product_shape(connection, V49_SCHEMA_HANDOFF.version)


@pytest.mark.parametrize(
    ("binding", "statement", "arguments"),
    [
        pytest.param(
            {},
            _FAIL_THE_LOCAL_ATTEMPT,
            (AgentAttemptFailureCode.CANDIDATE_UNCHANGED.value,),
            id="the-attempt-this-host-ran-itself",
        ),
        pytest.param(
            _RUNNER_BINDING,
            _FAIL_THE_RUNNER_ATTEMPT,
            (
                AgentAttemptFailureCode.CANDIDATE_UNCHANGED.value,
                _RUNNER_EVIDENCE_HASH,
            ),
            id="the-attempt-a-runner-returned-evidence-for",
        ),
    ],
)
def test_every_failed_transition_admits_the_unchanged_tree_only_after_the_v50_hop(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    binding: Mapping[str, object],
    statement: str,
    arguments: tuple[str, ...],
) -> None:
    """The vocabulary is one set, so both FAILED transitions gain the word at once.

    Asked from both carriers for the reason the candidate-capture hop was: a
    schema admitting a code in the table's CHECK but not in the transition
    trigger of one carrier would hold two answers to "which failure codes
    exist", and the newer of the two would be the quieter one.
    """

    database_path = tmp_path / "atelier.sqlite"
    _populated_v49_store_with(database_path, binding)

    with (
        sqlite3.connect(database_path) as connection,
        pytest.raises(sqlite3.IntegrityError),
    ):
        connection.execute(statement, arguments)

    assert main(["migrate", "--database", str(database_path)]) == 0
    capsys.readouterr()

    with sqlite3.connect(database_path) as connection:
        connection.execute(statement, arguments)
        connection.commit()
        assert connection.execute(
            "SELECT state, failure_code FROM agent_attempts"
        ).fetchone() == (
            "FAILED",
            AgentAttemptFailureCode.CANDIDATE_UNCHANGED.value,
        )


def _populated_v52_store_with(
    database_path: Path, binding: Mapping[str, object]
) -> None:
    """A published V52 store holding one armed attempt of the named carrier."""

    engine = create_canonical_engine(database_path)
    initialize_schema(engine)
    engine.dispose()
    with sqlite3.connect(database_path) as connection:
        _restore_v52_attempt_failure_vocabulary(connection)
        _write_armed_attempt(connection, binding)
        connection.execute("UPDATE atelier_schema_versions SET version = 52")
        connection.commit()
        _require_product_shape(connection, 52)


@pytest.mark.parametrize(
    ("binding", "statement", "arguments"),
    [
        pytest.param(
            {},
            _FAIL_THE_LOCAL_ATTEMPT,
            (AgentAttemptFailureCode.PRODUCED_VALUE_REFUSED.value,),
            id="the-attempt-this-host-ran-itself",
        ),
        pytest.param(
            _RUNNER_BINDING,
            _FAIL_THE_RUNNER_ATTEMPT,
            (
                AgentAttemptFailureCode.PRODUCED_VALUE_REFUSED.value,
                _RUNNER_EVIDENCE_HASH,
            ),
            id="the-attempt-a-runner-returned-evidence-for",
        ),
    ],
)
def test_every_failed_transition_admits_the_refused_value_only_after_the_v53_hop(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    binding: Mapping[str, object],
    statement: str,
    arguments: tuple[str, ...],
) -> None:
    """The vocabulary is one set, so both FAILED transitions gain the word at once.

    A store that admitted the code in the table's CHECK but not in the
    transition trigger of one carrier would hold two answers to "which failure
    codes exist", and the newer of the two would be the quieter one.
    """

    database_path = tmp_path / "atelier.sqlite"
    _populated_v52_store_with(database_path, binding)

    with (
        sqlite3.connect(database_path) as connection,
        pytest.raises(sqlite3.IntegrityError),
    ):
        connection.execute(statement, arguments)

    assert main(["migrate", "--database", str(database_path)]) == 0
    capsys.readouterr()

    with sqlite3.connect(database_path) as connection:
        connection.execute(statement, arguments)
        connection.commit()
        assert connection.execute(
            "SELECT state, failure_code FROM agent_attempts"
        ).fetchone() == (
            "FAILED",
            AgentAttemptFailureCode.PRODUCED_VALUE_REFUSED.value,
        )


_V53_EFFECT_REVISION = "5c" * 32
_V53_EFFECT_REQUEST_HASH = "5d" * 32
_V53_EFFECT_RESULT_HASH = "5e" * 32
_CLAIM_INTENT = (
    "INSERT INTO effect_intents VALUES ('v53-claim', 'v53-run', ?, ?, ?, "
    "'agent-claim-cli/0.12.0', '/checkout', '/usr/bin/agent-claim', "
    "'claim-work-item', 'PREPARED', 0, NULL)"
)


def _populated_v53_store(database_path: Path) -> tuple[object, ...]:
    """A published V53 store holding one confirmed open-pr effect."""

    engine = create_canonical_engine(database_path)
    initialize_schema(engine)
    engine.dispose()
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "INSERT INTO workflow_revisions VALUES (?, ?)",
            (_V53_EFFECT_REVISION, b"name: v53\nsteps: []\n"),
        )
        connection.execute(
            "INSERT INTO runs (run_id, bootstrap_workflow_id, revision_hash, "
            "workflow_format_version, current_node_id, current_round_ordinal, "
            "state, state_version, last_event_sequence) VALUES "
            "('v53-run', 'v53-bootstrap', ?, 1, 'effect', 1, 'STARTED', 1, 0)",
            (_V53_EFFECT_REVISION,),
        )
        binding = (
            "v53-effect",
            "v53-run",
            b"request",
            _V53_EFFECT_REQUEST_HASH,
            _V53_EFFECT_REVISION,
            "adapter-v1",
            "github",
            "github:owner/repository",
        )
        connection.execute(
            "INSERT INTO effect_intents VALUES (?, ?, ?, ?, ?, ?, ?, ?, "
            "'open-pr', 'CONFIRMED', 1, NULL)",
            binding,
        )
        connection.execute(
            "INSERT INTO effect_receipts VALUES (?, ?, ?, ?, ?, ?, ?, ?, "
            "'open-pr', 'pr/1', X'01', ?, 'ADAPTER_EXECUTION', NULL, NULL, NULL, "
            "NULL, NULL)",
            (*binding, _V53_EFFECT_RESULT_HASH),
        )
        _restore_v53_effect_operation_vocabulary(connection)
        connection.execute("UPDATE atelier_schema_versions SET version = 53")
        connection.commit()
        _require_product_shape(connection, 53)
        standing = connection.execute(
            "SELECT * FROM effect_receipts WHERE logical_key = 'v53-effect'"
        ).fetchone()
    assert standing is not None
    return standing


def test_the_effect_ledger_admits_a_work_item_claim_only_after_the_v54_hop(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The claim a run holds is an effect, so its operation is the ledger's word.

    A store that took a claim intent under a vocabulary its own CHECK does not
    name would hold a coordination fact no reader can trust, so the hop is what
    admits it -- and the effects already recorded cross it byte for byte.
    """

    database_path = tmp_path / "atelier.sqlite"
    standing_receipt = _populated_v53_store(database_path)
    claim_request = (b"claim request", _V53_EFFECT_REQUEST_HASH, _V53_EFFECT_REVISION)

    with (
        sqlite3.connect(database_path) as connection,
        pytest.raises(sqlite3.IntegrityError),
    ):
        connection.execute(_CLAIM_INTENT, claim_request)

    assert main(["migrate", "--database", str(database_path)]) == 0
    capsys.readouterr()

    with sqlite3.connect(database_path) as connection:
        connection.execute(_CLAIM_INTENT, claim_request)
        connection.commit()
        assert connection.execute(
            "SELECT operation_name, state FROM effect_intents "
            "WHERE logical_key = 'v53-claim'"
        ).fetchone() == (AdapterOperationName.CLAIM_WORK_ITEM.value, "PREPARED")
        assert (
            connection.execute(
                "SELECT * FROM effect_receipts WHERE logical_key = 'v53-effect'"
            ).fetchone()
            == standing_receipt
        )
        assert connection.execute(
            "SELECT version FROM atelier_schema_versions"
        ).fetchone() == (SCHEMA_VERSION,)


def _populated_v50_store_with(database_path: Path) -> None:
    """A published V50 store holding one started run standing at an armed attempt."""

    engine = create_canonical_engine(database_path)
    initialize_schema(engine)
    engine.dispose()
    with sqlite3.connect(database_path) as connection:
        _restore_v50_permission_ledger_predecessor(connection)
        _write_armed_attempt(connection)
        connection.execute("UPDATE atelier_schema_versions SET version = 50")
        connection.commit()
        _require_product_shape(connection, 50)


_DUMPED_ROW = "INSERT INTO"


def _dumped_rows(connection: sqlite3.Connection) -> frozenset[str]:
    """Every stored row this store dumps, except the version a hop itself raises.

    Rows rather than the whole dump: the statements around them carry table
    shapes, and a hop that republishes a table in a wider shape is expected to
    change those while leaving what is stored exactly as it was.
    """

    return frozenset(
        statement
        for statement in connection.iterdump()
        if statement.startswith(_DUMPED_ROW)
        and "atelier_schema_versions" not in statement
    )


def test_a_populated_v50_store_gains_the_permission_ledger_with_its_rows_intact(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The ledger arrives empty beside a live-shaped store that keeps everything.

    Nothing is backfilled: an attempt armed before this ledger existed answered
    whatever it answered under a policy nobody wrote down, and a row invented
    for it would be an authorisation that never authorised anything. The hops
    onto today's version run in the same command, so every stored row standing
    at V50 is still stored, unchanged, at the end of the chain.
    """

    database_path = tmp_path / "atelier.sqlite"
    _populated_v50_store_with(database_path)
    with sqlite3.connect(database_path) as connection:
        standing = _dumped_rows(connection)

    assert main(["migrate", "--database", str(database_path)]) == 0
    capsys.readouterr()

    with sqlite3.connect(database_path) as connection:
        assert _dumped_rows(connection) == standing
        assert connection.execute(
            "SELECT count(*) FROM permission_receipts"
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT version FROM atelier_schema_versions"
        ).fetchone() == (SCHEMA_VERSION,)
        _require_product_shape(connection, SCHEMA_VERSION)


def test_a_v50_store_already_carrying_a_permission_ledger_is_refused_whole(
    tmp_path: Path,
) -> None:
    """Rows this hop did not write are not rows it may build a ledger around."""

    database_path = tmp_path / "atelier.sqlite"
    _populated_v50_store_with(database_path)
    with sqlite3.connect(database_path) as connection:
        connection.execute("CREATE TABLE permission_receipts (mine TEXT)")
        connection.commit()

    with pytest.raises(StoreMigrationRefused, match="permission_receipts"):
        migrate_store(database_path)

    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT version FROM atelier_schema_versions"
        ).fetchone() == (50,)


def _populated_v51_store_with(database_path: Path) -> None:
    """A published V51 store holding one policy and one proposal an operator wrote."""

    engine = create_canonical_engine(database_path)
    initialize_schema(engine)
    published = PublishedRevision(RevisionKind.WORKFLOW, b"name: queue\n")
    lineage = CatalogLineage(published.kind, published.revision_hash)
    reference = WorkItemReference(ProjectId("studio"), TrackerItemReference("gh:1236"))
    with engine.begin() as connection:
        connection.execute(
            published_revisions.insert().values(
                kind=published.kind.value,
                revision_hash=published.revision_hash.value,
                document=published.document,
            )
        )
        connection.execute(
            catalog_lineages.insert().values(
                lineage_id=lineage.lineage_id.value,
                kind=published.kind.value,
                founding_revision_hash=published.revision_hash.value,
            )
        )
        connection.execute(
            queue_project_policy_revisions.insert().values(
                project_id=reference.project.value,
                revision_number=1,
                maximum_active_runs=2,
                automation_label="bereit",
            )
        )
        connection.execute(
            queue_items.insert().values(
                item_id=reference.item_id.value,
                project_id=reference.project.value,
                tracker_item_reference=reference.tracker_item.value,
                state=QueueItemState.OBSERVED.value,
                state_version=0,
            )
        )
        connection.execute(
            queue_proposal_revisions.insert().values(
                item_id=reference.item_id.value,
                proposal_revision=1,
                project_id=reference.project.value,
                priority_rank=7,
                workflow_lineage_id=lineage.lineage_id.value,
                automation_disposition=(
                    QueueAutomationDisposition.HUMAN_REQUIRED.value
                ),
                policy_revision=1,
                source=QueueProposalSource.OPERATOR.value,
            )
        )
        connection.execute(
            queue_items.update()
            .where(queue_items.c.item_id == reference.item_id.value)
            .values(
                state=QueueItemState.PROPOSED.value,
                state_version=1,
                current_proposal_revision=1,
            )
        )
    engine.dispose()
    with sqlite3.connect(database_path) as connection:
        _restore_v51_queue_defaults_predecessor(connection)
        connection.execute("UPDATE atelier_schema_versions SET version = 51")
        connection.commit()
        _require_product_shape(connection, 51)


def test_a_populated_v51_policy_and_proposal_cross_the_v52_hop(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The defaults arrive empty and the proposal keeps the source it was written by.

    A policy published before this hop named no defaults, so inventing a
    workflow for it would put a choice into the record that no operator made.
    A proposal, by contrast, has exactly one writer in that record -- the
    operator's own door -- so every stored row crosses as OPERATOR.
    """

    database_path = tmp_path / "atelier.sqlite"
    _populated_v51_store_with(database_path)

    assert main(["migrate", "--database", str(database_path)]) == 0
    capsys.readouterr()

    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT maximum_active_runs, automation_label, "
            "default_workflow_lineage_id, default_priority_rank, "
            "automation_disposition_default FROM queue_project_policy_revisions"
        ).fetchall() == [(2, "bereit", None, None, None)]
        assert connection.execute(
            "SELECT priority_rank, automation_disposition, policy_revision, source "
            "FROM queue_proposal_revisions"
        ).fetchall() == [(7, "HUMAN_REQUIRED", 1, "OPERATOR")]
        assert connection.execute(
            "SELECT state, state_version FROM queue_items"
        ).fetchall() == [("PROPOSED", 1)]
        assert connection.execute(
            "SELECT version FROM atelier_schema_versions"
        ).fetchone() == (SCHEMA_VERSION,)
        _require_product_shape(connection, SCHEMA_VERSION)


REDEEMED_AUTH = AuthProfileRevision(
    "max", 1, ProviderId("anthropic"), AuthMode.SUBSCRIPTION
)
REDEEMED_CONFIGURATION = AgentConfigurationRevision(
    "opus",
    REDEEMED_AUTH.revision_hash,
    AgentExecutorRevision("claude-cli/v1"),
    AgentExecutionCapability.HEADLESS,
    AgentConfigurationRevisionFormatVersion.V2,
)
REDEEMED_ROLE = AgentRole("builder")
REDEEMED_BINDING_SET = AgentBindingSet(
    (AgentBinding(REDEEMED_ROLE, REDEEMED_CONFIGURATION.revision_hash),)
)
REDEEMED_OPERATIONAL_IDENTITY = AgentExecutorOperationalIdentity("controlled-process")
REDEEMED_ANSWER = AgentExecutionResult(b'"done"')
REDEEMED_GRANT = DeclaredToolGrant(
    PublishedRevisionHash(Sha256Hash.of(b"grant/run-project-verification").value),
    ToolGrantCapability.RUN_PROJECT_VERIFICATION,
)
REDEEMED_COMMAND = ("/bin/sh", "-c", "run the project's own tests")
"""One redeemable success, composed of the product's own records.

Every hash below is derived by the record that owns it -- the auth profile's
from its own fields, the configuration's from the profile it names, the
request's from the binding it resolves, the receipt's from the request and the
answer. A fixture inventing any of them would build a store the product's own
readers reject, and would prove a hop against rows no runtime could have
written.
"""


@dataclass(frozen=True)
class WrittenAttempt:
    """The two identities a redemption of this attempt has to agree with."""

    attempt_id: str
    node_execution_id: str


def _redeemed_request() -> AgentExecutionRequestV2:
    """The exact request this fixture's attempt was made for."""

    revision = WorkflowRevision(b"name: bauhuette\n")
    return AgentExecutionRequestV2(
        NodeExecutionId.for_node(_ARMED_RUN, revision.revision_hash, _ARMED_NODE),
        _ARMED_RUN,
        revision.revision_hash,
        _ARMED_NODE,
        ResolvedAgentBinding(REDEEMED_ROLE, REDEEMED_CONFIGURATION, REDEEMED_AUTH),
        REDEEMED_OPERATIONAL_IDENTITY,
        b"build",
    )


def _redeemed_receipt(request: AgentExecutionRequestV2) -> AgentReceiptV2:
    return AgentReceiptV2.for_execution(
        request, REDEEMED_BINDING_SET.binding_set_hash, REDEEMED_ANSWER
    )


def _redeemed_redemption(
    request: AgentExecutionRequestV2, attempt_id: AgentAttemptId
) -> ToolRedemptionReceipt:
    return ToolRedemptionReceipt.of(
        request.node_execution_id,
        request.run_id,
        request.workflow_revision_hash,
        request.node_id,
        attempt_id,
        REDEEMED_GRANT,
        REDEEMED_COMMAND,
        0,
        Sha256Hash.of(b"all green"),
    )


def _publish_the_catalog_a_receipt_names(connection: sqlite3.Connection) -> None:
    """The catalog rows a receipt's five foreign keys point at.

    Written from the same records the receipt was composed from, so the hashes
    on both sides are one derivation rather than two that have to agree.
    """

    connection.execute(
        "INSERT INTO auth_profile_revisions (revision_hash, profile_id, "
        "revision_number, provider_id, auth_mode) VALUES (?, ?, ?, ?, ?)",
        (
            REDEEMED_AUTH.revision_hash.value,
            REDEEMED_AUTH.profile_id,
            REDEEMED_AUTH.revision_number,
            REDEEMED_AUTH.provider_id.value,
            REDEEMED_AUTH.auth_mode.value,
        ),
    )
    connection.execute(
        "INSERT INTO agent_configuration_revisions (revision_hash, model, "
        "auth_profile_revision_hash, executor_revision, revision_format_version, "
        "requested_capability) VALUES (?, ?, ?, ?, ?, ?)",
        (
            REDEEMED_CONFIGURATION.revision_hash.value,
            REDEEMED_CONFIGURATION.model,
            REDEEMED_CONFIGURATION.auth_profile_revision_hash.value,
            REDEEMED_CONFIGURATION.executor_revision.value,
            int(REDEEMED_CONFIGURATION.revision_format_version),
            REDEEMED_CONFIGURATION.requested_capability.value,
        ),
    )


def _bind_the_run_to_its_agent(connection: sqlite3.Connection) -> None:
    """The binding row a receipt's role and configuration are read through."""

    revision = WorkflowRevision(b"name: bauhuette\n")
    for binding in REDEEMED_BINDING_SET.bindings:
        connection.execute(
            "INSERT INTO run_agent_bindings (run_id, revision_hash, "
            "binding_set_hash, role, agent_configuration_revision_hash) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                _ARMED_RUN.value,
                revision.revision_hash.value,
                REDEEMED_BINDING_SET.binding_set_hash.value,
                binding.role.value,
                binding.agent_configuration_revision_hash.value,
            ),
        )


def _write_a_succeeded_attempt(
    connection: sqlite3.Connection, agent_binding_set_hash: str | None = None
) -> WrittenAttempt:
    """One succeeded attempt and the receipt it names, both as the product writes.

    The receipt is serialised through the same mapper the store uses, from a
    record composed by the contract that owns its hash, so what lands here is
    what a live run would have landed -- and what the product's own reader will
    accept when it is read back.
    """

    request = _redeemed_request()
    receipt = _redeemed_receipt(request)
    attempt_id = AgentAttemptId.for_execution(
        request.node_execution_id, request.request_hash, AGENT_ATTEMPT_ORDINAL
    )
    _write_armed_attempt(
        connection,
        {
            "attempt_id": attempt_id.value,
            "node_execution_id": request.node_execution_id.value,
            "request_hash": request.request_hash.value,
            "executor_operational_identity": (
                request.executor_operational_identity.value
            ),
        },
        agent_binding_set_hash=agent_binding_set_hash,
    )
    values = _agent_receipt_v2_values(receipt)
    connection.execute(
        f"INSERT INTO agent_receipts_v2 ({', '.join(values)}) "
        f"VALUES ({', '.join('?' for _ in values)})",
        tuple(values.values()),
    )
    # After the receipt, never before: the attempt's transition trigger admits
    # SUCCEEDED only where the receipt it names already stands and the two agree
    # on request hash and executor identity.
    connection.execute(
        "UPDATE agent_attempts SET state = 'SUCCEEDED', state_version = 2, "
        "receipt_hash = ? WHERE attempt_id = ?",
        (receipt.receipt_hash.value, attempt_id.value),
    )
    return WrittenAttempt(attempt_id.value, request.node_execution_id.value)


def _bend_the_succeeded_attempt(connection: sqlite3.Connection, statement: str) -> None:
    """Leave a succeeded attempt in a state its own transition trigger refuses.

    Stores written *here* never hold these, which is why the hop still has to
    read for them: nothing about V38's shape stops a store written elsewhere
    from carrying one, and `foreign_key_check` sees three rows that all exist.
    """

    connection.execute("DROP TRIGGER agent_attempts_state_transition")
    connection.execute(statement)
    # V38's own text, not today's: this store stands at V38, and putting the
    # widened trigger back would break the fingerprint the fixture then asserts.
    connection.execute(_V38_AGENT_ATTEMPT_TRIGGERS["agent_attempts_state_transition"])


def _write_a_redemption(
    connection: sqlite3.Connection,
    *,
    attempt_id: str,
    exit_code: int = 0,
    **disagreements: str,
) -> None:
    """One redemption row, serialised through the mapper the store itself uses.

    The record is composed by the contract that owns its hash, so what lands is
    the derivation production performs rather than a value a fixture chose. Any
    `disagreements` are applied to the *serialised row*, never to the record --
    the contract refuses to hold a redemption whose execution identity does not
    follow from its run, revision and node, and that refusal is the reason these
    rows can only ever come from somewhere else.
    """

    request = _redeemed_request()
    redeemed = ToolRedemptionReceipt.of(
        request.node_execution_id,
        request.run_id,
        request.workflow_revision_hash,
        request.node_id,
        AgentAttemptId(attempt_id),
        REDEEMED_GRANT,
        REDEEMED_COMMAND,
        exit_code,
        Sha256Hash.of(b"all green"),
    )
    values = dict(_tool_redemption_values(redeemed)) | disagreements
    connection.execute(
        f"INSERT INTO tool_redemptions ({', '.join(values)}) "
        f"VALUES ({', '.join('?' for _ in values)})",
        tuple(values.values()),
    )


def _create_v38_store_that_cannot_be_re_owned(
    database_path: Path,
    write: Callable[[sqlite3.Connection, WrittenAttempt], None],
    *,
    succeeded: bool = False,
) -> None:
    """A published V38 store holding a redemption V39 has to refuse.

    Each of these rows is one a *foreign* V38 store could hold and this
    product's own never would: its two foreign keys point at different tables
    and constrain each other not at all. They are written directly for exactly
    that reason -- driving them through this product is impossible, which is the
    whole reason the hop cannot assume them away.
    """

    engine = create_canonical_engine(database_path)
    initialize_schema(engine)
    engine.dispose()
    with sqlite3.connect(database_path) as connection:
        _restore_v39_configuration_tables(connection)
        _revert_the_candidate_capture_code(connection)
        if succeeded:
            # No catalog is published here: every store this builds is one
            # the hop refuses at its preflight, which reads long before the
            # migration asks the store about its keys.
            connection.execute("PRAGMA foreign_keys=OFF")
            attempt = _write_a_succeeded_attempt(connection)
        else:
            _write_armed_attempt(connection)
            armed = _armed_attempt_values(
                WorkflowRevision(b"name: bauhuette\n").revision_hash
            )
            attempt = WrittenAttempt(
                str(armed["attempt_id"]), str(armed["node_execution_id"])
            )
        write(connection, attempt)
        connection.execute(
            "UPDATE atelier_schema_versions SET version = ?",
            (V38_SCHEMA_HANDOFF.version,),
        )
        connection.commit()
        _require_product_shape(connection, V38_SCHEMA_HANDOFF.version)


@pytest.mark.parametrize(
    ("write", "refusal"),
    [
        pytest.param(
            lambda connection, attempt: [
                _write_a_redemption(connection, attempt_id=attempt.attempt_id),
                # A second row for the same attempt under a different execution:
                # V38 keys this table by the node execution, so this is what a
                # duplicate attempt owner actually looks like there.
                _write_a_redemption(
                    connection,
                    attempt_id=attempt.attempt_id,
                    node_execution_id="d4" * 32,
                    receipt_hash="d5" * 32,
                ),
            ],
            "more than one tool redemption",
            id="two-redemptions-claiming-one-attempt",
        ),
        pytest.param(
            lambda connection, attempt: _write_a_redemption(
                connection, attempt_id="f0" * 32
            ),
            "do not belong to a succeeded attempt",
            id="a-redemption-naming-no-stored-attempt",
        ),
        pytest.param(
            lambda connection, attempt: _write_a_redemption(
                connection, attempt_id=attempt.attempt_id
            ),
            "do not belong to a succeeded attempt",
            id="a-redemption-whose-attempt-never-succeeded",
        ),
        pytest.param(
            lambda connection, attempt: _write_a_redemption(
                connection,
                attempt_id=attempt.attempt_id,
                node_execution_id="e1" * 32,
            ),
            "do not belong to a succeeded attempt",
            id="a-redemption-describing-another-execution-than-its-attempt",
        ),
        pytest.param(
            lambda connection, attempt: _write_a_redemption(
                connection, attempt_id=attempt.attempt_id, exit_code=1
            ),
            "exited",
            id="a-redemption-of-a-command-that-failed",
        ),
    ],
)
def test_a_redemption_the_v39_hop_cannot_re_own_refuses_the_store_unaltered(
    tmp_path: Path,
    write: Callable[[sqlite3.Connection, WrittenAttempt], None],
    refusal: str,
) -> None:
    """V38's two foreign keys never made these impossible, so the hop reads first.

    Each would end with the proof of a check somewhere it does not belong --
    collided onto one key, or moved onto an attempt that never ran the command.
    None of them is a store this product wrote, which is exactly why the hop
    cannot assume them away. It refuses the whole store rather than repairing
    it, and leaves every byte where it was: guessing which half of a
    contradiction to keep is how evidence gets quietly rewritten.
    """

    database_path = tmp_path / "atelier.sqlite"
    _create_v38_store_that_cannot_be_re_owned(database_path, write)
    before = database_path.read_bytes()

    with (
        sqlite3.connect(database_path) as connection,
        pytest.raises(StoreMigrationRefused, match=refusal),
    ):
        _refuse_redemptions_that_cannot_be_re_owned(connection)

    assert main(["migrate", "--database", str(database_path)]) == 1
    assert database_path.read_bytes() == before
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT version FROM atelier_schema_versions"
        ).fetchone() == (V38_SCHEMA_HANDOFF.version,)


@pytest.mark.parametrize(
    ("write", "refusal"),
    [
        pytest.param(
            lambda connection, attempt: _write_a_redemption(
                connection,
                attempt_id=attempt.attempt_id,
                node_id="a-node-this-attempt-never-ran",
            ),
            "do not belong to a succeeded attempt",
            id="a-redemption-naming-other-work-than-its-attempt-and-receipt",
        ),
        pytest.param(
            lambda connection, attempt: (
                _bend_the_succeeded_attempt(
                    connection,
                    f"UPDATE agent_attempts SET receipt_hash = '{'a1' * 32}'",
                ),
                _write_a_redemption(connection, attempt_id=attempt.attempt_id),
            ),
            "hang from no agent receipt",
            id="an-attempt-pointing-at-a-receipt-that-is-not-the-one-found",
        ),
        pytest.param(
            lambda connection, attempt: (
                _bend_the_succeeded_attempt(
                    connection,
                    f"UPDATE agent_attempts SET request_hash = '{'b2' * 32}'",
                ),
                _write_a_redemption(connection, attempt_id=attempt.attempt_id),
            ),
            "hang from no agent receipt",
            id="an-attempt-and-receipt-disagreeing-on-the-request-they-answered",
        ),
        pytest.param(
            lambda connection, attempt: (
                _bend_the_succeeded_attempt(
                    connection,
                    "UPDATE agent_attempts SET executor_operational_identity = "
                    "'another-executor'",
                ),
                _write_a_redemption(connection, attempt_id=attempt.attempt_id),
            ),
            "hang from no agent receipt",
            id="an-attempt-and-receipt-disagreeing-on-the-executor-that-ran",
        ),
    ],
)
def test_three_rows_that_describe_different_work_refuse_the_v39_hop(
    tmp_path: Path,
    write: Callable[[sqlite3.Connection, WrittenAttempt], None],
    refusal: str,
) -> None:
    """A redemption, its attempt and its receipt have to be about one execution.

    V38 never made them so: its two foreign keys point at different tables and
    constrain each other not at all, so all three can name different work and
    `foreign_key_check` still comes back clean. Carrying such a row would move
    the proof of a check onto an execution that never ran it -- the quietest way
    this hop could corrupt the evidence it exists to preserve -- so the
    agreement is read in full rather than inferred from the keys.

    The request hash and the executor identity are in that agreement because
    V38's *own* success trigger required both before an attempt could reach
    SUCCEEDED. A store where they disagree is one that arrived at that state
    without passing through the door, and nothing downstream would notice.
    """

    database_path = tmp_path / "atelier.sqlite"
    _create_v38_store_that_cannot_be_re_owned(database_path, write, succeeded=True)
    before = database_path.read_bytes()

    with (
        sqlite3.connect(database_path) as connection,
        pytest.raises(StoreMigrationRefused, match=refusal),
    ):
        _refuse_redemptions_that_cannot_be_re_owned(connection)

    assert main(["migrate", "--database", str(database_path)]) == 1
    assert database_path.read_bytes() == before
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT version FROM atelier_schema_versions"
        ).fetchone() == (V38_SCHEMA_HANDOFF.version,)


def _create_v38_store_from_a_real_redemption(database_path: Path) -> WrittenAttempt:
    """A published V38 store holding one redemption that can honestly be re-owned.

    Every row the hop reads is here and consistent: the catalog the receipt
    names, the receipt itself, the succeeded attempt naming it, and the
    redemption of a check that passed. This is the store the hop must carry
    rather than refuse, so it has to survive the foreign-key check the migration
    runs -- which is why the catalog is published rather than assumed.
    """

    engine = create_canonical_engine(database_path)
    initialize_schema(engine)
    engine.dispose()
    with sqlite3.connect(database_path) as connection:
        _restore_v39_configuration_tables(connection)
        _revert_the_candidate_capture_code(connection)
        _publish_the_catalog_a_receipt_names(connection)
        attempt = _write_a_succeeded_attempt(
            connection, REDEEMED_BINDING_SET.binding_set_hash.value
        )
        _bind_the_run_to_its_agent(connection)
        _write_a_redemption(connection, attempt_id=attempt.attempt_id)
        connection.execute(
            "UPDATE atelier_schema_versions SET version = ?",
            (V38_SCHEMA_HANDOFF.version,),
        )
        connection.commit()
        _require_product_shape(connection, V38_SCHEMA_HANDOFF.version)
    return attempt


def _read_as_production_does(database_path: Path) -> tuple[object, object]:
    """The receipt and the redemption, read back by the product's own readers.

    Both recompute the hash the row carries and refuse it where the content
    disagrees, so asking them is how a test says "these are rows a live runtime
    could have written" rather than rows that merely fit the columns.
    """

    with sqlite3.connect(database_path) as connection:
        connection.row_factory = sqlite3.Row
        receipt = connection.execute("SELECT * FROM agent_receipts_v2").fetchone()
        redemption = connection.execute("SELECT * FROM tool_redemptions").fetchone()
    return (
        _agent_receipt_v2_from_record(dict(receipt)),
        _tool_redemption_from_record(dict(redemption)),
    )


def test_a_v38_redemption_crosses_the_v39_hop_owned_by_its_own_attempt(
    tmp_path: Path,
) -> None:
    """The proof a check left is carried to its attempt, unchanged and entire."""

    database_path = tmp_path / "atelier.sqlite"
    written = _create_v38_store_from_a_real_redemption(database_path)
    before_readers = _read_as_production_does(database_path)
    columns = (
        "attempt_id, node_execution_id, run_id, workflow_revision_hash, node_id, "
        "tool_revision_hash, capability, command, exit_code, "
        "standard_output_hash, receipt_hash"
    )
    with sqlite3.connect(database_path) as connection:
        predecessor_row = connection.execute(
            f"SELECT {columns} FROM tool_redemptions"
        ).fetchone()
    assert predecessor_row is not None

    report = migrate_store(database_path)

    assert (report.source_version, report.target_version) == (
        V38_SCHEMA_HANDOFF.version,
        SCHEMA_VERSION,
    )
    with sqlite3.connect(database_path) as connection:
        assert (
            connection.execute(f"SELECT {columns} FROM tool_redemptions").fetchone()
            == predecessor_row
        )
        key = connection.execute(
            "SELECT name FROM pragma_table_info('tool_redemptions') WHERE pk = 1"
        ).fetchone()
    assert key == ("attempt_id",)
    assert predecessor_row[0] == written.attempt_id
    # The rows the product's own readers accepted before the hop are the rows
    # they accept after it: a carry that changed any byte a hash covers would
    # be refused here rather than noticed years later.
    assert _read_as_production_does(database_path) == before_readers
