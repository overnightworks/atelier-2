from __future__ import annotations

import sqlalchemy as sa
from dbos import DBOSClient, EnqueueOptions
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DatabaseError, OperationalError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError

from atelier2.adapters.dbos.effect_store import (
    command_snapshot_from_record,
    intent_snapshot_from_record,
)
from atelier2.adapters.dbos.names import QUEUE_NAME, RECONCILE_WORKFLOW_NAME
from atelier2.adapters.dbos.runtime import DbosRuntimeSettings
from atelier2.adapters.dbos.schema import effect_intents, reconcile_commands
from atelier2.adapters.dbos.transactions import canonical_write_transaction
from atelier2.adapters.dbos.workflow_ids import reconcile_workflow_id_for
from atelier2.contracts.effects import (
    EffectIntentState,
    OperatorFoundEffect,
    ReconcileCommand,
    ReconcileCommandSnapshot,
    ReconcileCommandState,
)
from atelier2.ports.durable_runs import DurableStateCorrupt, DurableWriteUnavailable
from atelier2.ports.effects import (
    DurableReconciliationCommandConflict,
    DurableReconciliationCreated,
    DurableReconciliationDeterminationConflict,
    DurableReconciliationExisting,
    DurableReconciliationResult,
    DurableReconciliationTargetMissing,
)


class DbosEffectReconcileCommander:
    def __init__(self, engine: Engine, settings: DbosRuntimeSettings) -> None:
        self._engine = engine
        self._settings = settings

    def submit_result(self, command: ReconcileCommand) -> DurableReconciliationResult:
        client: DBOSClient | None = None
        try:
            client = DBOSClient(
                system_database_engine=self._engine, use_listen_notify=False
            )
            with canonical_write_transaction(self._engine) as connection:
                intent_record = (
                    connection.execute(
                        sa.select(effect_intents).where(
                            effect_intents.c.logical_key
                            == command.intent_reference.binding.logical_key.value
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                if intent_record is None:
                    return DurableReconciliationTargetMissing()
                intent_snapshot = intent_snapshot_from_record(intent_record)
                if intent_snapshot.intent.reference != command.intent_reference:
                    return DurableReconciliationCommandConflict()

                accepted = (
                    intent_snapshot.state is EffectIntentState.WAITING_RECONCILIATION
                    and intent_snapshot.state_version
                    == command.expected_intent_state_version
                )
                state = (
                    ReconcileCommandState.PENDING
                    if accepted
                    else ReconcileCommandState.REJECTED_CONFLICT
                )
                if accepted:
                    intent_snapshot.intent.resolve_reconciliation(
                        command, intent_snapshot.state_version
                    )
                inserted = self._insert_command(connection, command, state)
                stored_record = (
                    connection.execute(
                        sa.select(reconcile_commands).where(
                            reconcile_commands.c.command_id == command.command_id.value
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                if stored_record is None:
                    raise RuntimeError("inserted reconcile command is not readable")
                stored_intent_record = (
                    connection.execute(
                        sa.select(effect_intents).where(
                            effect_intents.c.logical_key == stored_record["logical_key"]
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                if stored_intent_record is None:
                    raise RuntimeError("reconcile command intent is not readable")
                snapshot = command_snapshot_from_record(
                    stored_record,
                    intent_snapshot_from_record(stored_intent_record).intent,
                )
                if inserted.rowcount == 0:
                    return _stored_command_result(snapshot, command)
                if not accepted:
                    return DurableReconciliationCreated(snapshot)

                updated = connection.execute(
                    effect_intents.update()
                    .where(
                        effect_intents.c.logical_key
                        == command.intent_reference.binding.logical_key.value,
                        effect_intents.c.state
                        == EffectIntentState.WAITING_RECONCILIATION.value,
                        effect_intents.c.state_version
                        == command.expected_intent_state_version.value,
                    )
                    .values(
                        state=EffectIntentState.RECONCILING.value,
                        state_version=command.expected_intent_state_version.value + 1,
                        reconciliation_owner_command_id=command.command_id.value,
                    )
                )
                if updated.rowcount != 1:
                    raise RuntimeError(
                        "serialized reconciliation update lost ownership"
                    )
                workflow_id = reconcile_workflow_id_for(command.command_id)
                options: EnqueueOptions = {
                    "workflow_name": RECONCILE_WORKFLOW_NAME,
                    "queue_name": QUEUE_NAME,
                    "workflow_id": workflow_id,
                    "app_version": self._settings.application_version,
                }
                client.enqueue_in_transaction(
                    connection,
                    options,
                    command.command_id.value,
                    command.intent_reference.binding.workflow_revision_hash.value,
                )
                return DurableReconciliationCreated(snapshot)
        except (OperationalError, PoolTimeoutError):
            return DurableWriteUnavailable()
        except (ValueError, RuntimeError, DatabaseError):
            return DurableStateCorrupt()
        finally:
            if client is not None:
                client.destroy()

    @staticmethod
    def _insert_command(
        connection: sa.Connection,
        command: ReconcileCommand,
        state: ReconcileCommandState,
    ) -> sa.CursorResult[object]:
        determination = command.determination
        found = isinstance(determination, OperatorFoundEffect)
        return connection.execute(
            reconcile_commands.insert()
            .prefix_with("OR IGNORE")
            .values(
                command_id=command.command_id.value,
                logical_key=command.intent_reference.binding.logical_key.value,
                expected_intent_version=command.expected_intent_state_version.value,
                determination="FOUND" if found else "AUTHORITATIVE_NOT_FOUND",
                actor=command.actor.value,
                evidence=command.evidence,
                found_effect_id=determination.effect_id.value if found else None,
                found_result=determination.result.payload if found else None,
                found_result_hash=(
                    determination.result.payload_hash.value if found else None
                ),
                state=state.value,
            )
        )


def _stored_command_result(
    snapshot: ReconcileCommandSnapshot, command: ReconcileCommand
) -> (
    DurableReconciliationExisting
    | DurableReconciliationDeterminationConflict
    | DurableReconciliationCommandConflict
):
    """What a command id the store already holds means for this submission."""

    stored = snapshot.command
    if stored == command:
        return DurableReconciliationExisting(snapshot)
    if (
        stored.command_id == command.command_id
        and stored.intent_reference == command.intent_reference
        and stored.expected_intent_state_version
        == command.expected_intent_state_version
        and stored.actor == command.actor
        and stored.evidence == command.evidence
        and stored.determination != command.determination
    ):
        return DurableReconciliationDeterminationConflict()
    return DurableReconciliationCommandConflict()
