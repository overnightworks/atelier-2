from __future__ import annotations

import struct
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import sqlalchemy as sa
from dbos import DBOSClient
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import DatabaseError, OperationalError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError

from atelier2.adapters.dbos.agent_effect_grants import (
    agent_node_redeems_platform_effect,
)
from atelier2.adapters.dbos.bound_reads import one_record
from atelier2.adapters.dbos.effect_store import receipt_from_record
from atelier2.adapters.dbos.instants import record_run_started
from atelier2.adapters.dbos.names import QUEUE_NAME, WORKFLOW_NAME
from atelier2.adapters.dbos.node_records import (
    node_receipt_from_record,
    persist_bound_node_executions,
)
from atelier2.adapters.dbos.run_store import (
    bootstrap_node_for_snapshot,
    load_run_orders,
)
from atelier2.adapters.dbos.run_transitions import (
    event_from_record,
    load_graph,
    run_from_record_with_bindings,
)
from atelier2.adapters.dbos.runtime import DbosRuntimeSettings
from atelier2.adapters.dbos.schema import (
    agent_receipts_v2,
    context_packages_v3,
    effect_receipts,
    node_execution_requests_v3,
    node_receipts_v3,
    run_agent_bindings,
    run_configuration_revisions,
    run_events,
    run_fork_effect_fences,
    run_fork_reused_nodes,
    run_forks,
    run_inputs_v3,
    runs,
)
from atelier2.adapters.dbos.transactions import canonical_write_transaction
from atelier2.adapters.dbos.workflow_ids import fork_bootstrap_workflow_id_for
from atelier2.contracts.agent_modes import AgentModeMismatch, agent_mode_mismatch
from atelier2.contracts.effect_requests import OpenPullRequest
from atelier2.contracts.effects import ConfirmationSource, LogicalEffectKey
from atelier2.contracts.executions import (
    NodeExecutionId,
    RunEventKind,
    logical_effect_key_for_node,
)
from atelier2.contracts.hashing import Sha256Hash, unframe
from atelier2.contracts.node_records_v3 import (
    DeclaredContextPackage,
    DeclaredContextPackageHash,
    InputEnvelope,
    NodeReceiptHash,
    PersistedReceiptDisposition,
    ProjectedDeliveryStatus,
)
from atelier2.contracts.revisions_v3 import PublishedRevisionHash, RevisionKind
from atelier2.contracts.run_bindings import RunV3
from atelier2.contracts.run_configuration_v3 import (
    ReferenceSite,
    ResolvedReference,
    RunConfigurationRevision,
    RunConfigurationRevisionHash,
)
from atelier2.contracts.run_forks import (
    MAXIMUM_RUN_FORK_EVIDENCE_RECORDS,
    RunFork,
    RunForkCommandId,
    RunForkEffectFence,
    RunForkReusedNode,
    successor_run_id_for,
)
from atelier2.contracts.runs import TERMINAL_RUN_STATES, RunId, WorkflowRevisionHash
from atelier2.contracts.workflows_v3 import (
    ActionNodeV3,
    AgentNodeV3,
    NodeOutputSource,
    VersionedReference,
    WaitNodeV3,
    WorkflowGraphV3,
    linear_successor_id,
)
from atelier2.ports.agent_executions import AgentExecutorKey, AgentExecutorRegistry
from atelier2.ports.durable_run_forks import (
    DurableRunForkCapabilityUnavailable,
    DurableRunForkCommandConflict,
    DurableRunForkCreated,
    DurableRunForkExecutorUnavailable,
    DurableRunForkExisting,
    DurableRunForkLoopUnsupported,
    DurableRunForkNodeMissing,
    DurableRunForkOriginMissing,
    DurableRunForkOriginNotTerminal,
    DurableRunForkPrefixNotReusable,
    DurableRunForkResult,
    DurableRunForkStateCorrupt,
    DurableRunForkWriteUnavailable,
    ForkRunRequest,
)


class _PrefixNotReusable(RuntimeError):
    pass


class DbosRunForkStore:
    """One serialized fork decision, including its DBOS enqueue."""

    def __init__(
        self,
        engine: Engine,
        settings: DbosRuntimeSettings,
        agent_executor_registry: AgentExecutorRegistry,
    ) -> None:
        self._engine = engine
        self._settings = settings
        self._agent_executor_registry = agent_executor_registry

    def fork_run(self, request: ForkRunRequest) -> DurableRunForkResult:
        try:
            command_id = RunForkCommandId.for_request(
                request.origin_run_id, request.idempotency_key
            )
        except ValueError:
            return DurableRunForkCommandConflict()
        client: DBOSClient | None = None
        try:
            client = DBOSClient(
                system_database_engine=self._engine, use_listen_notify=False
            )
            with canonical_write_transaction(self._engine) as connection:
                existing = _stored_fork_for_command(connection, command_id)
                if existing is not None:
                    if (
                        existing.origin_run_id != request.origin_run_id
                        or existing.restart_from_node_id != request.restart_from_node_id
                    ):
                        return DurableRunForkCommandConflict()
                    successor = validate_stored_fork(connection, existing)
                    return DurableRunForkExisting(existing, successor)

                restart = _fork_origin(connection, request)
                if not isinstance(restart, _ForkOrigin):
                    return restart
                origin, graph = restart.run, restart.graph
                refusal = self._binding_refusal(origin, graph)
                if refusal is not None:
                    return refusal

                run_configuration = _load_run_configuration(connection, origin)
                orders = load_run_orders(connection, (origin.run_id.value,)).get(
                    origin.run_id.value, ()
                )
                line = _linear_node_ids(graph)
                target_index = line.index(request.restart_from_node_id)
                reused = tuple(
                    _resolve_reused_node(connection, origin, graph, node_id)
                    for node_id in line[:target_index]
                )
                reuse_by_node = {entry.node_id: entry for entry in reused}
                fences = _effect_fences(connection, origin, graph, line[target_index:])
                successor_run_id = successor_run_id_for(command_id)
                inherited_inputs = _inherited_inputs(
                    connection,
                    graph,
                    line[target_index:],
                    reuse_by_node,
                )
                fork = RunFork(
                    command_id,
                    origin.run_id,
                    restart.terminal_hash,
                    successor_run_id,
                    origin.revision_hash,
                    origin.run_configuration_revision_hash,
                    request.restart_from_node_id,
                    reused,
                    fences,
                )
                workflow_id = fork_bootstrap_workflow_id_for(successor_run_id)
                connection.execute(
                    runs.insert().values(
                        run_id=successor_run_id.value,
                        bootstrap_workflow_id=workflow_id,
                        revision_hash=origin.revision_hash.value,
                        workflow_format_version=3,
                        agent_binding_set_hash=origin.binding_set_hash.value,
                        current_node_id=request.restart_from_node_id,
                        current_round_ordinal=1,
                        state="STARTED",
                        state_version=0,
                        last_event_sequence=0,
                        terminal_hash=None,
                        run_configuration_revision_hash=(
                            origin.run_configuration_revision_hash.value
                        ),
                    )
                )
                _copy_bindings(connection, origin.run_id, successor_run_id)
                _copy_orders(connection, origin.run_id, successor_run_id)
                connection.execute(
                    run_forks.insert().values(
                        command_id=fork.command_id.value,
                        origin_run_id=fork.origin_run_id.value,
                        origin_terminal_hash=fork.origin_terminal_hash.value,
                        successor_run_id=fork.successor_run_id.value,
                        workflow_revision_hash=fork.workflow_revision_hash.value,
                        run_configuration_revision_hash=(
                            fork.run_configuration_revision_hash.value
                        ),
                        restart_from_node_id=fork.restart_from_node_id,
                        fork_hash=fork.fork_hash.value,
                    )
                )
                if reused:
                    connection.execute(
                        run_fork_reused_nodes.insert(),
                        [
                            _reused_node_values(successor_run_id, position, entry)
                            for position, entry in enumerate(reused)
                        ],
                    )
                if fences:
                    connection.execute(
                        run_fork_effect_fences.insert(),
                        [
                            _effect_fence_values(successor_run_id, position, entry)
                            for position, entry in enumerate(fences)
                        ],
                    )
                persist_bound_node_executions(
                    connection,
                    successor_run_id,
                    origin.revision_hash,
                    graph,
                    run_configuration,
                    orders,
                    node_ids=frozenset(line[target_index:]),
                    inherited_inputs=inherited_inputs,
                )
                record_run_started(connection, successor_run_id.value)
                client.enqueue_in_transaction(
                    connection,
                    {
                        "workflow_name": WORKFLOW_NAME,
                        "queue_name": QUEUE_NAME,
                        "workflow_id": workflow_id,
                        "app_version": self._settings.application_version,
                    },
                    successor_run_id.value,
                    origin.revision_hash.value,
                )
                successor = _load_successor(connection, successor_run_id)
                if (
                    bootstrap_node_for_snapshot(connection, successor, graph)
                    != request.restart_from_node_id
                ):
                    raise RuntimeError("fork successor bootstrap target disagrees")
                return DurableRunForkCreated(fork, successor)
        except _PrefixNotReusable:
            return DurableRunForkPrefixNotReusable()
        except (OperationalError, PoolTimeoutError) as error:
            return DurableRunForkWriteUnavailable(str(error))
        except (ValueError, RuntimeError, DatabaseError, KeyError, struct.error):
            return DurableRunForkStateCorrupt()
        finally:
            if client is not None:
                client.destroy()

    def _binding_refusal(
        self, origin: RunV3, graph: WorkflowGraphV3
    ) -> (
        DurableRunForkExecutorUnavailable
        | DurableRunForkCapabilityUnavailable
        | AgentModeMismatch
        | None
    ):
        """What stops the origin's bindings from carrying a successor, if anything.

        A successor inherits the origin's bindings unchanged, so it answers to
        the same judgement a start does: a missing or incapable executor, and a
        node bound outside its mode, refuse the fork before anything is written.
        """
        for binding in origin.agent_bindings:
            key = AgentExecutorKey(
                binding.auth_profile.provider_id,
                binding.configuration.executor_revision,
            )
            if not self._agent_executor_registry.contains(key):
                return DurableRunForkExecutorUnavailable()
            capability = binding.configuration.requested_capability
            if capability not in self._agent_executor_registry.declared_capabilities(
                key
            ):
                return DurableRunForkCapabilityUnavailable()
            if not self._agent_executor_registry.is_startable(
                key, capability, binding.configuration.revision_hash
            ):
                return DurableRunForkExecutorUnavailable()
        return agent_mode_mismatch(graph, origin.agent_bindings)


@dataclass(frozen=True)
class _ForkOrigin:
    """A finished V3 run and the loop-free graph a fork restarts from."""

    run: RunV3
    terminal_hash: Sha256Hash
    graph: WorkflowGraphV3


def _fork_origin(
    connection: Connection, request: ForkRunRequest
) -> (
    _ForkOrigin
    | DurableRunForkOriginMissing
    | DurableRunForkOriginNotTerminal
    | DurableRunForkStateCorrupt
    | DurableRunForkLoopUnsupported
    | DurableRunForkNodeMissing
):
    origin_record = one_record(
        connection,
        sa.select(runs).where(runs.c.run_id == request.origin_run_id.value),
    )
    if origin_record is None:
        return DurableRunForkOriginMissing()
    origin = run_from_record_with_bindings(connection, origin_record)
    if origin.state not in TERMINAL_RUN_STATES:
        return DurableRunForkOriginNotTerminal()
    if not isinstance(origin, RunV3) or origin.terminal_hash is None:
        return DurableRunForkStateCorrupt()
    graph = load_graph(connection, origin.revision_hash)
    if not isinstance(graph, WorkflowGraphV3):
        return DurableRunForkStateCorrupt()
    if graph.loops:
        return DurableRunForkLoopUnsupported()
    try:
        graph.node(request.restart_from_node_id)
    except KeyError:
        return DurableRunForkNodeMissing()
    return _ForkOrigin(origin, origin.terminal_hash, graph)


def _linear_node_ids(graph: WorkflowGraphV3) -> tuple[str, ...]:
    node_id = graph.entry_node_ids[0]
    walked: list[str] = []
    while True:
        walked.append(node_id)
        if node_id in graph.sink_node_ids:
            return tuple(walked)
        node_id = linear_successor_id(graph, node_id)


def _load_run_configuration(
    connection: Connection, origin: RunV3
) -> RunConfigurationRevision:
    preimage = connection.scalar(
        sa.select(run_configuration_revisions.c.preimage).where(
            run_configuration_revisions.c.revision_hash
            == origin.run_configuration_revision_hash.value
        )
    )
    if preimage is None:
        raise RuntimeError("origin run configuration is missing")
    fields = unframe(bytes(preimage), "run-configuration-revision/v1")
    if len(fields) != 3:
        raise ValueError("run configuration preimage has the wrong arity")
    references = tuple(
        _resolved_reference(field)
        for field in unframe(fields[2], "run-configuration-references/v1")
    )
    configuration = RunConfigurationRevision(
        WorkflowRevisionHash(fields[0].decode("ascii")),
        origin.binding_set_hash.__class__(fields[1].decode("ascii")),
        references,
    )
    if (
        configuration.preimage != bytes(preimage)
        or configuration.revision_hash != origin.run_configuration_revision_hash
        or configuration.workflow_revision_hash != origin.revision_hash
        or configuration.binding_set_hash != origin.binding_set_hash
    ):
        raise RuntimeError("origin run configuration binding disagrees")
    return configuration


def _resolved_reference(payload: bytes) -> ResolvedReference:
    fields = unframe(payload, "run-configuration-reference/v1")
    if len(fields) != 8:
        raise ValueError("run configuration reference has the wrong arity")
    chain = tuple(
        VersionedReference(
            ref=entry_fields[0].decode("utf-8"),
            revision=entry_fields[1].decode("utf-8"),
        )
        for entry in unframe(fields[4], "reference-chain/v1")
        if len(entry_fields := unframe(entry, "reference-chain-entry/v1")) == 2
    )
    return ResolvedReference(
        ReferenceSite(
            fields[1].decode("utf-8"),
            fields[2].decode("utf-8") or None,
            fields[3].decode("utf-8") or None,
            chain,
        ),
        RevisionKind(fields[0].decode("ascii")),
        VersionedReference(
            ref=fields[5].decode("utf-8"), revision=fields[6].decode("utf-8")
        ),
        PublishedRevisionHash(fields[7].decode("ascii")),
    )


def _resolve_reused_node(
    connection: Connection,
    origin: RunV3,
    graph: WorkflowGraphV3,
    node_id: str,
) -> RunForkReusedNode:
    inherited = one_record(
        connection,
        sa.select(run_fork_reused_nodes).where(
            run_fork_reused_nodes.c.successor_run_id == origin.run_id.value,
            run_fork_reused_nodes.c.node_id == node_id,
            run_fork_reused_nodes.c.round_ordinal == 1,
        ),
    )
    if inherited is not None:
        reference = _reused_node_from_record(inherited)
        _validate_reused_source(connection, reference, graph)
        return reference
    execution_id = NodeExecutionId.for_node(
        origin.run_id, origin.revision_hash, node_id
    )
    event_kind = _successful_event_kind(graph.node(node_id))
    event_record = one_record(
        connection,
        sa.select(run_events).where(
            run_events.c.node_execution_id == execution_id.value,
            run_events.c.event_kind == event_kind.value,
        ),
    )
    receipt_record = one_record(
        connection,
        sa.select(node_receipts_v3).where(
            node_receipts_v3.c.node_execution_id == execution_id.value
        ),
    )
    if event_record is None or receipt_record is None:
        raise _PrefixNotReusable(
            f"strict predecessor {node_id!r} has no reusable success fact"
        )
    event = event_from_record(event_record)
    receipt = node_receipt_from_record(connection, receipt_record)
    if receipt.disposition is not PersistedReceiptDisposition.SUCCEEDED:
        raise _PrefixNotReusable(f"strict predecessor {node_id!r} did not succeed")
    reference = RunForkReusedNode(
        node_id,
        1,
        origin.run_id,
        origin.revision_hash,
        execution_id,
        event.event_hash,
        receipt.receipt_hash,
        receipt.context_package_hash,
        (
            None
            if event.agent_receipt_hash is None
            else Sha256Hash(event.agent_receipt_hash.value)
        ),
    )
    _validate_reused_source(connection, reference, graph)
    return reference


def _successful_event_kind(node: object) -> RunEventKind:
    match node:
        case AgentNodeV3():
            return RunEventKind.AGENT_COMPLETED
        case WaitNodeV3():
            return RunEventKind.WAIT_ANSWERED
        case ActionNodeV3():
            return RunEventKind.ACTION_COMPLETED
        case _:
            raise TypeError("fork prefix contains an unsupported node kind")


def _validate_reused_source(
    connection: Connection, reference: RunForkReusedNode, graph: WorkflowGraphV3
) -> None:
    event_record = one_record(
        connection,
        sa.select(run_events).where(
            run_events.c.run_id == reference.source_run_id.value,
            run_events.c.revision_hash == reference.source_workflow_revision_hash.value,
            run_events.c.node_execution_id == reference.source_node_execution_id.value,
            run_events.c.event_hash == reference.source_event_hash.value,
        ),
    )
    receipt_record = one_record(
        connection,
        sa.select(node_receipts_v3).where(
            node_receipts_v3.c.node_execution_id
            == reference.source_node_execution_id.value,
            node_receipts_v3.c.receipt_hash == reference.source_receipt_hash.value,
        ),
    )
    request_record = one_record(
        connection,
        sa.select(node_execution_requests_v3).where(
            node_execution_requests_v3.c.node_execution_id
            == reference.source_node_execution_id.value
        ),
    )
    package_manifest = connection.scalar(
        sa.select(context_packages_v3.c.manifest).where(
            context_packages_v3.c.package_hash
            == reference.source_declared_context_package_hash.value
        )
    )
    if (
        event_record is None
        or receipt_record is None
        or request_record is None
        or package_manifest is None
    ):
        raise RuntimeError("reused source evidence is incomplete")
    event = event_from_record(event_record)
    receipt = node_receipt_from_record(connection, receipt_record)
    package = DeclaredContextPackage(bytes(package_manifest))
    if (
        event.event_kind != _successful_event_kind(graph.node(reference.node_id))
        or event.node_id != reference.node_id
        or event.round_ordinal != reference.round_ordinal
        or receipt.disposition is not PersistedReceiptDisposition.SUCCEEDED
        or str(request_record["request_hash"]) != receipt.request_hash.value
        or str(request_record["context_package_hash"])
        != receipt.context_package_hash.value
        or package.package_hash != reference.source_declared_context_package_hash
    ):
        raise RuntimeError("reused source evidence disagrees")
    if reference.source_agent_receipt_hash is not None:
        agent_record = connection.scalar(
            sa.select(agent_receipts_v2.c.receipt_hash).where(
                agent_receipts_v2.c.node_execution_id
                == reference.source_node_execution_id.value
            )
        )
        if agent_record != reference.source_agent_receipt_hash.value:
            raise RuntimeError("reused source agent receipt disagrees")


def _effect_fences(
    connection: Connection,
    origin: RunV3,
    graph: WorkflowGraphV3,
    node_ids: Sequence[str],
) -> tuple[RunForkEffectFence, ...]:
    fences: list[RunForkEffectFence] = []
    for node_id in node_ids:
        node = graph.node(node_id)
        effect_bearing = isinstance(node, ActionNodeV3) or (
            isinstance(node, AgentNodeV3)
            and agent_node_redeems_platform_effect(connection, node)
        )
        receipt_record = _effective_receipt_record(connection, origin, graph, node_id)
        if receipt_record is None:
            if effect_bearing and _effective_node_succeeded(
                connection, origin, graph, node_id
            ):
                raise RuntimeError(
                    f"succeeded effect-bearing node {node_id!r} has no receipt"
                )
            continue
        ultimate = _ultimate_effect_receipt_record(connection, receipt_record)
        receipt = receipt_from_record(ultimate)
        fences.append(
            RunForkEffectFence(
                node_id,
                1,
                receipt.intent.binding.logical_key,
                receipt.intent.binding.run_id,
                receipt.intent.binding.workflow_revision_hash,
                receipt.result.payload_hash,
            )
        )
    return tuple(fences)


def _effective_node_succeeded(
    connection: Connection,
    origin: RunV3,
    graph: WorkflowGraphV3,
    node_id: str,
) -> bool:
    execution_id = NodeExecutionId.for_node(
        origin.run_id, origin.revision_hash, node_id
    )
    direct = connection.scalar(
        sa.select(sa.literal(True)).where(
            sa.exists().where(
                run_events.c.run_id == origin.run_id.value,
                run_events.c.revision_hash == origin.revision_hash.value,
                run_events.c.node_execution_id == execution_id.value,
                run_events.c.event_kind
                == _successful_event_kind(graph.node(node_id)).value,
            )
        )
    )
    if direct is not None:
        return True
    inherited = one_record(
        connection,
        sa.select(run_fork_reused_nodes).where(
            run_fork_reused_nodes.c.successor_run_id == origin.run_id.value,
            run_fork_reused_nodes.c.node_id == node_id,
        ),
    )
    if inherited is None:
        return False
    _validate_reused_source(connection, _reused_node_from_record(inherited), graph)
    return True


def _effective_receipt_record(
    connection: Connection,
    origin: RunV3,
    graph: WorkflowGraphV3,
    node_id: str,
) -> Mapping[Any, Any] | None:
    logical_key = logical_effect_key_for_node(
        origin.run_id, origin.revision_hash, node_id
    )
    direct = one_record(
        connection,
        sa.select(effect_receipts).where(
            effect_receipts.c.logical_key == logical_key.value
        ),
    )
    if direct is not None:
        return direct
    inherited = one_record(
        connection,
        sa.select(run_fork_reused_nodes).where(
            run_fork_reused_nodes.c.successor_run_id == origin.run_id.value,
            run_fork_reused_nodes.c.node_id == node_id,
        ),
    )
    if inherited is None:
        return None
    reference = _reused_node_from_record(inherited)
    _validate_reused_source(connection, reference, graph)
    source_event = one_record(
        connection,
        sa.select(run_events).where(
            run_events.c.event_hash == str(inherited["source_event_hash"])
        ),
    )
    if source_event is None:
        raise RuntimeError("reused effect source event is missing")
    if source_event["receipt_logical_key"] is None:
        if reference.source_agent_receipt_hash is None:
            return None
        source_logical_key = logical_effect_key_for_node(
            reference.source_run_id,
            reference.source_workflow_revision_hash,
            reference.node_id,
            reference.round_ordinal,
        )
        agent_output = connection.execute(
            sa.select(
                agent_receipts_v2.c.output_bytes,
                agent_receipts_v2.c.output_hash,
            ).where(
                agent_receipts_v2.c.node_execution_id
                == reference.source_node_execution_id.value,
                agent_receipts_v2.c.receipt_hash
                == reference.source_agent_receipt_hash.value,
            )
        ).one_or_none()
        source_receipt = one_record(
            connection,
            sa.select(effect_receipts).where(
                effect_receipts.c.logical_key == source_logical_key.value,
                effect_receipts.c.run_id == reference.source_run_id.value,
                effect_receipts.c.workflow_revision_hash
                == reference.source_workflow_revision_hash.value,
            ),
        )
        if source_receipt is None:
            return None
        receipt = receipt_from_record(source_receipt)
        try:
            request = OpenPullRequest.from_canonical_bytes(
                receipt.intent.request.payload
            )
        except (TypeError, ValueError) as error:
            raise RuntimeError(
                "reused agent effect receipt has no typed open-pr request"
            ) from error
        if agent_output is None or request.body.encode("utf-8") != bytes(
            agent_output.output_bytes
        ):
            raise RuntimeError("reused agent effect receipt disagrees with its output")
        return source_receipt
    if source_event["receipt_result_hash"] is None:
        raise RuntimeError("reused action effect source is incomplete")
    if reference.source_agent_receipt_hash is not None:
        raise RuntimeError("reused agent effect carries an action receipt binding")
    return (
        connection.execute(
            sa.select(effect_receipts).where(
                effect_receipts.c.logical_key
                == str(source_event["receipt_logical_key"]),
                effect_receipts.c.run_id == str(inherited["source_run_id"]),
                effect_receipts.c.workflow_revision_hash
                == str(inherited["source_workflow_revision_hash"]),
                effect_receipts.c.result_hash
                == str(source_event["receipt_result_hash"]),
            )
        )
        .mappings()
        .one_or_none()
    )


def _ultimate_effect_receipt_record(
    connection: Connection, record: Mapping[Any, Any]
) -> Mapping[Any, Any]:
    seen: set[tuple[str, str, str, str]] = set()
    current = record
    while str(current["confirmation_source"]) == ConfirmationSource.FORK_REFERENCE:
        identity = (
            str(current["fork_source_logical_key"]),
            str(current["fork_source_run_id"]),
            str(current["fork_source_workflow_revision_hash"]),
            str(current["fork_source_result_hash"]),
        )
        if identity in seen or any(value == "None" for value in identity):
            raise RuntimeError("fork receipt reference cycle or incomplete source")
        seen.add(identity)
        current = (
            connection.execute(
                sa.select(effect_receipts).where(
                    effect_receipts.c.logical_key == identity[0],
                    effect_receipts.c.run_id == identity[1],
                    effect_receipts.c.workflow_revision_hash == identity[2],
                    effect_receipts.c.result_hash == identity[3],
                )
            )
            .mappings()
            .one()
        )
    receipt_from_record(current)
    return current


def _inherited_inputs(
    connection: Connection,
    graph: WorkflowGraphV3,
    node_ids: Sequence[str],
    reused: Mapping[str, RunForkReusedNode],
) -> dict[str, tuple[InputEnvelope, ...]]:
    inherited: dict[str, tuple[InputEnvelope, ...]] = {}
    for node_id in node_ids:
        node = graph.node(node_id)
        envelopes: list[InputEnvelope] = []
        for input_entry in node.inputs:
            source = input_entry.source
            if not isinstance(source, NodeOutputSource) or source.node not in reused:
                continue
            reference = reused[source.node]
            event_record = (
                connection.execute(
                    sa.select(run_events).where(
                        run_events.c.event_hash == reference.source_event_hash.value
                    )
                )
                .mappings()
                .one()
            )
            producer = graph.node(source.node)
            declared = next(
                output for output in producer.outputs if output.name == source.output
            )
            envelopes.append(
                InputEnvelope(
                    ProjectedDeliveryStatus.SUCCEEDED,
                    input_entry.name,
                    PublishedRevisionHash(declared.schema_reference.revision),
                    Sha256Hash(str(event_record["payload_hash"])),
                    None,
                    reference.source_event_hash,
                    reference.source_receipt_hash,
                )
            )
        if envelopes:
            inherited[node_id] = tuple(envelopes)
    return inherited


def _copy_bindings(connection: Connection, origin: RunId, successor: RunId) -> None:
    records = tuple(
        connection.execute(
            sa.select(run_agent_bindings).where(
                run_agent_bindings.c.run_id == origin.value
            )
        ).mappings()
    )
    if records:
        connection.execute(
            run_agent_bindings.insert(),
            [
                {
                    **dict(record),
                    "run_id": successor.value,
                }
                for record in records
            ],
        )


def _copy_orders(connection: Connection, origin: RunId, successor: RunId) -> None:
    records = tuple(
        connection.execute(
            sa.select(run_inputs_v3).where(run_inputs_v3.c.run_id == origin.value)
        ).mappings()
    )
    if records:
        connection.execute(
            run_inputs_v3.insert(),
            [{**dict(record), "run_id": successor.value} for record in records],
        )


def _reused_node_values(
    successor: RunId, position: int, entry: RunForkReusedNode
) -> dict[str, object]:
    return {
        "successor_run_id": successor.value,
        "position": position,
        "node_id": entry.node_id,
        "round_ordinal": entry.round_ordinal,
        "source_run_id": entry.source_run_id.value,
        "source_workflow_revision_hash": entry.source_workflow_revision_hash.value,
        "source_node_execution_id": entry.source_node_execution_id.value,
        "source_event_hash": entry.source_event_hash.value,
        "source_receipt_hash": entry.source_receipt_hash.value,
        "source_declared_context_package_hash": (
            entry.source_declared_context_package_hash.value
        ),
        "source_agent_receipt_hash": (
            None
            if entry.source_agent_receipt_hash is None
            else entry.source_agent_receipt_hash.value
        ),
    }


def _effect_fence_values(
    successor: RunId, position: int, entry: RunForkEffectFence
) -> dict[str, object]:
    return {
        "successor_run_id": successor.value,
        "position": position,
        "node_id": entry.node_id,
        "round_ordinal": entry.round_ordinal,
        "source_logical_key": entry.source_logical_key.value,
        "source_run_id": entry.source_run_id.value,
        "source_workflow_revision_hash": entry.source_workflow_revision_hash.value,
        "source_result_hash": entry.source_result_hash.value,
    }


def _stored_fork_for_command(
    connection: Connection, command_id: RunForkCommandId
) -> RunFork | None:
    record = one_record(
        connection,
        sa.select(run_forks).where(run_forks.c.command_id == command_id.value),
    )
    if record is None:
        return None
    reused = tuple(
        _reused_node_from_record(item)
        for item in connection.execute(
            sa.select(run_fork_reused_nodes)
            .where(
                run_fork_reused_nodes.c.successor_run_id
                == str(record["successor_run_id"])
            )
            .order_by(run_fork_reused_nodes.c.position)
            .limit(MAXIMUM_RUN_FORK_EVIDENCE_RECORDS + 1)
        ).mappings()
    )
    fences = tuple(
        RunForkEffectFence(
            str(item["node_id"]),
            int(item["round_ordinal"]),
            LogicalEffectKey(str(item["source_logical_key"])),
            RunId(str(item["source_run_id"])),
            WorkflowRevisionHash(str(item["source_workflow_revision_hash"])),
            Sha256Hash(str(item["source_result_hash"])),
        )
        for item in connection.execute(
            sa.select(run_fork_effect_fences)
            .where(
                run_fork_effect_fences.c.successor_run_id
                == str(record["successor_run_id"])
            )
            .order_by(run_fork_effect_fences.c.position)
            .limit(MAXIMUM_RUN_FORK_EVIDENCE_RECORDS + 1)
        ).mappings()
    )
    fork = RunFork(
        RunForkCommandId(str(record["command_id"])),
        RunId(str(record["origin_run_id"])),
        Sha256Hash(str(record["origin_terminal_hash"])),
        RunId(str(record["successor_run_id"])),
        WorkflowRevisionHash(str(record["workflow_revision_hash"])),
        RunConfigurationRevisionHash(str(record["run_configuration_revision_hash"])),
        str(record["restart_from_node_id"]),
        reused,
        fences,
    )
    if fork.fork_hash.value != str(record["fork_hash"]):
        raise RuntimeError("durable run fork hash disagrees")
    return fork


def validate_stored_fork(connection: Connection, fork: RunFork) -> RunV3:
    """Reconstruct and prove one stored fork before any caller projects it."""

    origin = _load_successor(connection, fork.origin_run_id)
    successor = _load_successor(connection, fork.successor_run_id)
    _validate_stored_fork_header(connection, fork, successor)
    graph = load_graph(connection, fork.workflow_revision_hash)
    if not isinstance(graph, WorkflowGraphV3) or graph.loops:
        raise RuntimeError("stored fork graph is no longer executable")
    line = _linear_node_ids(graph)
    target_index = line.index(fork.restart_from_node_id)
    if tuple(entry.node_id for entry in fork.reused_nodes) != line[:target_index]:
        raise RuntimeError("stored fork prefix disagrees with its graph")
    try:
        expected_reused_nodes = tuple(
            _resolve_reused_node(connection, origin, graph, node_id)
            for node_id in line[:target_index]
        )
    except _PrefixNotReusable as error:
        raise RuntimeError(
            "stored fork prefix evidence is no longer reusable"
        ) from error
    if expected_reused_nodes != fork.reused_nodes:
        raise RuntimeError("stored fork prefix evidence disagrees")
    if _effect_fences(connection, origin, graph, line[target_index:]) != (
        fork.effect_fences
    ):
        raise RuntimeError("stored fork effect evidence disagrees")
    return successor


def _reused_node_from_record(record: Mapping[Any, Any]) -> RunForkReusedNode:
    agent_receipt = record["source_agent_receipt_hash"]
    return RunForkReusedNode(
        str(record["node_id"]),
        int(record["round_ordinal"]),
        RunId(str(record["source_run_id"])),
        WorkflowRevisionHash(str(record["source_workflow_revision_hash"])),
        NodeExecutionId(str(record["source_node_execution_id"])),
        Sha256Hash(str(record["source_event_hash"])),
        NodeReceiptHash(str(record["source_receipt_hash"])),
        DeclaredContextPackageHash(str(record["source_declared_context_package_hash"])),
        None if agent_receipt is None else Sha256Hash(str(agent_receipt)),
    )


def _load_successor(connection: Connection, run_id: RunId) -> RunV3:
    record = (
        connection.execute(sa.select(runs).where(runs.c.run_id == run_id.value))
        .mappings()
        .one_or_none()
    )
    if record is None:
        raise RuntimeError("run fork successor is missing")
    run = run_from_record_with_bindings(connection, record)
    if not isinstance(run, RunV3):
        raise TypeError("run fork successor is not a V3 run")
    return run


def _validate_stored_fork_header(
    connection: Connection, fork: RunFork, successor: RunV3
) -> None:
    origin = one_record(
        connection,
        sa.select(
            runs.c.terminal_hash,
            runs.c.revision_hash,
            runs.c.agent_binding_set_hash,
            runs.c.run_configuration_revision_hash,
        ).where(runs.c.run_id == fork.origin_run_id.value),
    )
    bootstrap_workflow_id = connection.scalar(
        sa.select(runs.c.bootstrap_workflow_id).where(
            runs.c.run_id == fork.successor_run_id.value
        )
    )
    if (
        origin is None
        or str(origin["terminal_hash"]) != fork.origin_terminal_hash.value
        or str(origin["revision_hash"]) != fork.workflow_revision_hash.value
        or str(origin["agent_binding_set_hash"]) != successor.binding_set_hash.value
        or str(origin["run_configuration_revision_hash"])
        != fork.run_configuration_revision_hash.value
        or successor.revision_hash != fork.workflow_revision_hash
        or successor.run_configuration_revision_hash
        != fork.run_configuration_revision_hash
        or bootstrap_workflow_id
        != fork_bootstrap_workflow_id_for(fork.successor_run_id)
    ):
        raise RuntimeError("durable run fork header disagrees with its runs")
    orders = load_run_orders(
        connection, (fork.origin_run_id.value, fork.successor_run_id.value)
    )
    if orders.get(fork.origin_run_id.value, ()) != orders.get(
        fork.successor_run_id.value, ()
    ):
        raise RuntimeError("durable run fork orders disagree with its origin")
