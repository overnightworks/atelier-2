from __future__ import annotations

from dataclasses import dataclass, replace
from typing import assert_never

import sqlalchemy as sa
from dbos import DBOSClient
from sqlalchemy.engine import Connection, Engine, RowMapping
from sqlalchemy.exc import DatabaseError, OperationalError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError

from atelier2.adapters.dbos.agent_catalog import (
    agent_configuration_from_record,
    auth_profile_from_record,
)
from atelier2.adapters.dbos.artifact_store import read_stored_artifact
from atelier2.adapters.dbos.bound_reads import one_record
from atelier2.adapters.dbos.catalog_store import DbosCatalogStore
from atelier2.adapters.dbos.host_configuration import model_configuration_snapshot
from atelier2.adapters.dbos.instants import record_run_started
from atelier2.adapters.dbos.names import QUEUE_NAME, WORKFLOW_NAME
from atelier2.adapters.dbos.node_records import persist_bound_node_executions
from atelier2.adapters.dbos.run_store import (
    entry_node_of,
    load_published_schema_document,
)
from atelier2.adapters.dbos.run_transitions import run_from_record_with_bindings
from atelier2.adapters.dbos.runtime import DbosRuntimeSettings
from atelier2.adapters.dbos.schema import (
    agent_configuration_revisions,
    auth_profile_revisions,
    run_agent_bindings,
    run_configuration_revisions,
    run_inputs_v3,
    runs,
    workflow_revisions,
)
from atelier2.adapters.dbos.transactions import canonical_write_transaction
from atelier2.adapters.dbos.workflow_ids import bootstrap_workflow_id_for
from atelier2.adapters.yaml_workflows import parse_workflow_document
from atelier2.application.evaluate_executability import (
    DocumentNotExecutable,
    ExecutableDocument,
    evaluate_executability,
)
from atelier2.application.refusals import DurableStateCorrupt as RegistryCorrupt
from atelier2.application.refusals import ReadUnavailable as RegistryUnavailable
from atelier2.application.resolve_start_bindings import (
    AuthProfileMissingForConfiguration,
    agent_role_completeness_refusal,
    cast_unbound_roles,
    resolve_start_bindings,
    undeclared_agent_role_refusal,
)
from atelier2.application.role_candidates import registered_configurations
from atelier2.contracts.agents import (
    AgentBindingSet,
    AgentConfigurationRevision,
    AgentConfigurationRevisionHash,
    AuthProfileRevision,
    ResolvedAgentBinding,
)
from atelier2.contracts.artifacts import MAXIMUM_ARTIFACT_BYTES
from atelier2.contracts.hashing import Sha256Hash
from atelier2.contracts.host_configuration import (
    HostModelConfigurationSnapshot,
    ModelRegistryBytesDisagree,
    ProjectModelDefaultsBytesDisagree,
)
from atelier2.contracts.node_records_v3 import RunInput
from atelier2.contracts.orders import (
    ArtifactOrderValue,
    InlineOrderValue,
    ObservedWorkItemOrderValue,
    WorkItemOrderValue,
)
from atelier2.contracts.revisions_v3 import PublishedRevisionHash
from atelier2.contracts.run_bindings import RunV3
from atelier2.contracts.run_configuration_v3 import (
    ResolvedReference,
    RunConfigurationRevision,
)
from atelier2.contracts.runs import (
    FIRST_ROUND_ORDINAL,
    RunId,
    RunState,
    WorkflowRevision,
    WorkflowRevisionHash,
)
from atelier2.contracts.schemas_v3 import (
    MAXIMUM_INSTANCE_DOCUMENT_BYTES,
    InstanceRefused,
    SchemaRefused,
    read_authored_instance_document,
    read_schema_document,
)
from atelier2.contracts.work_items import (
    WORK_ITEM_ORDER_SCHEMA_REVISION,
    WorkItemScopeMalformed,
    read_work_item_order_document,
    work_item_order_document,
)
from atelier2.contracts.workflow_formats import WorkflowFormatVersion
from atelier2.contracts.workflows_v3 import (
    AnyWorkflowDocument,
    WorkflowGraphV3,
)
from atelier2.ports.agent_executions import AgentExecutorRegistry
from atelier2.ports.durable_run_forks import DurableRunForkResult, ForkRunRequest
from atelier2.ports.durable_runs import (
    AnyStartPublishedRunRequest,
    AuthoredOrder,
    DurableInvalidAgentBindings,
    DurablePublishedRunResult,
    DurableRunCreated,
    DurableRunExisting,
    DurableRunFormatNotExecutable,
    DurableRunIdentityConflict,
    DurableRunRevisionMissing,
    DurableStateCorrupt,
    DurableUncastAgentRoles,
    DurableV3StartInputRefused,
    DurableWorkItemOrderUnread,
    DurableWriteUnavailable,
    StartPublishedRunRequest,
    StartPublishedRunRequestV2,
    StartPublishedRunRequestV3,
    V3InputRefusal,
)
from atelier2.ports.workflow_revisions import (
    DurableRevisionCollision,
    DurableRevisionCreated,
    DurableRevisionExisting,
    DurableRevisionPublicationResult,
)

_OR_IGNORE = "OR IGNORE"


def _supplied_orders(request: AnyStartPublishedRunRequest) -> tuple[RunInput, ...]:
    """The orders this start carries, for a request shape that can carry any."""
    return request.run_inputs if isinstance(request, StartPublishedRunRequestV3) else ()


def _authored_orders(request: AnyStartPublishedRunRequest) -> tuple[AuthoredOrder, ...]:
    """The name-and-bytes form a caller can honestly supply."""
    return request.orders if isinstance(request, StartPublishedRunRequestV3) else ()


def _pin_authored_orders(
    connection: Connection,
    graph: WorkflowGraphV3,
    run_configuration: RunConfigurationRevision,
    authored: tuple[AuthoredOrder, ...],
) -> tuple[RunInput, ...] | DurableV3StartInputRefused:
    """Bind each authored order to the schema the document pinned, and to its bytes.

    A caller does not name a schema hash, so a typo is the order the operator
    meant rather than a SCHEMA_MISMATCH. An order naming an artifact also
    becomes bytes here, before the run exists, so an address nobody published
    refuses the start by name rather than failing an unforeseen attempt later.
    """
    declared = {entry.name for entry in graph.graph_inputs}
    duplicated = _duplicated_order_refusal([order.name for order in authored])
    if duplicated is not None:
        return duplicated
    pinned_orders: list[RunInput] = []
    for order in authored:
        if order.name not in declared:
            return DurableV3StartInputRefused(order.name, V3InputRefusal.UNDECLARED)
        pinned = _resolved_graph_input_schema(run_configuration, order.name)
        if pinned is None:
            return DurableV3StartInputRefused(
                order.name,
                V3InputRefusal.SCHEMA_MISMATCH,
                "the document pinned nothing",
            )
        value = _order_value_bytes(connection, order, pinned)
        if isinstance(value, DurableV3StartInputRefused):
            return value
        pinned_orders.append(RunInput(order.name, pinned, value))
    return tuple(pinned_orders)


def _order_value_bytes(
    connection: Connection, order: AuthoredOrder, pinned: PublishedRevisionHash
) -> bytes | DurableV3StartInputRefused:
    """The exact bytes one authored order is, whichever way it was supplied.

    The inline bound bites here rather than at the schema reading below, so
    every route's refusal names the same door. A work item is the one value
    whose *kind* must be declared: it is stored only under the house schema,
    and a malformed scope-list token in its body refuses the order by name,
    never as corruption.
    """
    match order.value:
        case InlineOrderValue(content):
            if len(content) > MAXIMUM_INSTANCE_DOCUMENT_BYTES:
                return DurableV3StartInputRefused(
                    order.name,
                    V3InputRefusal.VALUE_REFUSED,
                    f"{len(content)} inline bytes exceeds "
                    f"{MAXIMUM_INSTANCE_DOCUMENT_BYTES}; publish material this "
                    "large as an artifact and order its address",
                )
            return content
        case ArtifactOrderValue(artifact_hash):
            stored = read_stored_artifact(connection, artifact_hash)
            if stored is None:
                return DurableV3StartInputRefused(
                    order.name,
                    V3InputRefusal.UNKNOWN_ARTIFACT,
                    f"no artifact carries the address {artifact_hash.value}",
                )
            return stored.content
        case ObservedWorkItemOrderValue(revision):
            if pinned != WORK_ITEM_ORDER_SCHEMA_REVISION:
                return DurableV3StartInputRefused(
                    order.name,
                    V3InputRefusal.SCHEMA_MISMATCH,
                    "this order is a work item, and the document pinned "
                    f"{pinned.value} instead of the work item schema "
                    f"{WORK_ITEM_ORDER_SCHEMA_REVISION.value}",
                )
            try:
                content = work_item_order_document(revision)
            except WorkItemScopeMalformed as malformed:
                return DurableV3StartInputRefused(
                    order.name, V3InputRefusal.VALUE_REFUSED, str(malformed)
                )
            if len(content) > MAXIMUM_INSTANCE_DOCUMENT_BYTES:
                return DurableV3StartInputRefused(
                    order.name,
                    V3InputRefusal.VALUE_REFUSED,
                    f"the item {revision.item.value} reads as {len(content)} "
                    f"inline bytes, which exceeds {MAXIMUM_INSTANCE_DOCUMENT_BYTES}",
                )
            return content
        case WorkItemOrderValue():
            raise RuntimeError("an unread work item order never reaches the pin")
        case _ as unreachable:
            assert_never(unreachable)


def _refused_order(
    connection: Connection,
    graph: WorkflowGraphV3,
    run_configuration: RunConfigurationRevision,
    orders: tuple[RunInput, ...],
) -> DurableV3StartInputRefused | None:
    """The first order this start cannot honour, named by the input it is about.

    ADR 0006 binds a root run to every `graph_input` its document declares, and a
    missing one refuses the start naming the input. The same door refuses an order
    the document never declared, one whose schema is not the schema the document
    pinned, and a value that schema does not admit -- each before any row exists,
    because an order nobody could read is not a run to clean up.
    """
    declared = {entry.name: entry for entry in graph.graph_inputs}
    supplied = {order.name: order for order in orders}
    duplicated = _duplicated_order_refusal([order.name for order in orders])
    if duplicated is not None:
        return duplicated
    for name in declared:
        if name not in supplied:
            return DurableV3StartInputRefused(name, V3InputRefusal.MISSING)
    for name, order in supplied.items():
        if name not in declared:
            return DurableV3StartInputRefused(name, V3InputRefusal.UNDECLARED)
        refused = _refused_supplied_order(connection, run_configuration, order)
        if refused is not None:
            return refused
    return None


def _duplicated_order_refusal(names: list[str]) -> DurableV3StartInputRefused | None:
    seen: set[str] = set()
    for name in names:
        if name in seen:
            return DurableV3StartInputRefused(
                name,
                V3InputRefusal.DUPLICATED,
                "one name answers one order, and this start supplied it twice",
            )
        seen.add(name)
    return None


def _refused_supplied_order(
    connection: Connection, run_configuration: RunConfigurationRevision, order: RunInput
) -> DurableV3StartInputRefused | None:
    """A schema that is not readable as a schema is not answered here. The reference
    that pins it was already resolved to build the configuration this reads, and
    that resolution refuses unusable schema bytes at the document -- so reaching
    this point means the pinned schema is one this product enforces.
    """
    pinned = _resolved_graph_input_schema(run_configuration, order.name)
    if pinned != order.schema_revision:
        return DurableV3StartInputRefused(
            order.name,
            V3InputRefusal.SCHEMA_MISMATCH,
            f"the document pinned {'nothing' if pinned is None else pinned.value}",
        )
    is_work_item = order.schema_revision == WORK_ITEM_ORDER_SCHEMA_REVISION
    if is_work_item and read_work_item_order_document(order.value) is None:
        # The schema alone admits a shape; this door admits only the whole
        # document a tracker read produces, digest and all. That is what
        # makes every stored row under this schema one whose identity can
        # be read back as the item it names.
        return DurableV3StartInputRefused(
            order.name,
            V3InputRefusal.VALUE_REFUSED,
            "this input is a work item, so its value is one the start read: "
            "name the item instead of writing its bytes",
        )
    document = load_published_schema_document(connection, order.schema_revision.value)
    if document is None:
        raise RuntimeError("a resolved schema revision is absent from the store")
    match read_schema_document(document):
        case SchemaRefused() as unreadable:
            raise RuntimeError(f"a resolved schema revision is not one: {unreadable}")
        case schema:
            # The value is judged as it will be read, which for an ordered
            # artifact is its full content: the route it arrived by already
            # bounded it, and refusing it a second time under the inline
            # bound would refuse what the artifact door admitted. It is
            # judged as an *authored* value, not a produced one: a caller
            # supplying an order owes no JSON-encoding promise an executor
            # would, so a `"string"`-typed schema reads this order's raw
            # text directly (`schemas_v3.read_authored_instance_document`).
            verdict = read_authored_instance_document(
                order.value, schema, MAXIMUM_ARTIFACT_BYTES
            )
    if isinstance(verdict, InstanceRefused):
        return DurableV3StartInputRefused(
            order.name, V3InputRefusal.VALUE_REFUSED, str(verdict), verdict.violation
        )
    return None


def _requested_order_identity(
    name: str, schema_hash: str, value: bytes
) -> tuple[str, str, str]:
    """What makes two starts of one run the same start, for one order a caller asks.

    Bytes answer that for material a caller supplied. They cannot answer it for
    a work item: its value is a read of a moving object, so a second start of
    the same run legitimately reads different bytes for the same item. Under the
    house work-item schema the identity is therefore the item the value names --
    which is inside the value already, so nothing durable has to grow a column
    to remember it.

    Bytes under that schema that are not the complete document a read produces
    are identified by their bytes here, which makes them differ from every
    stored work item: `_refused_order` refuses them a moment later anyway, and
    until then the honest answer to "is this the same start" is no.
    """

    if schema_hash == WORK_ITEM_ORDER_SCHEMA_REVISION.value:
        document = read_work_item_order_document(value)
        if document is not None:
            return (name, schema_hash, document.reference.value)
    return (name, schema_hash, Sha256Hash.of(value).value)


def _unread_work_items(request: AnyStartPublishedRunRequest) -> bool:
    """Whether this start still names a work item nobody has read."""

    return any(
        isinstance(order.value, WorkItemOrderValue)
        for order in _authored_orders(request)
    )


def _unread_order_identities(
    connection: Connection,
    request: AnyStartPublishedRunRequest,
    run_configuration: RunConfigurationRevision | None,
) -> tuple[tuple[str, str, str], ...] | None:
    """What this start's orders identify, before any work item has been read.

    An unread work item identifies the item it names; everything beside it
    identifies its bytes, exactly as `_requested_orders` reads them -- an
    artifact included, because resolving one is a read of this store and not of
    a tracker, so a start that mixes the two is still answerable without
    reaching for the platform.

    `None` says this start cannot be compared here at all -- a pin the document
    does not carry, or an artifact this store never saw. The caller then lets
    the ordinary path answer, which names the real refusal instead of guessing
    a conflict.
    """

    if run_configuration is None:
        return None
    identities: list[tuple[str, str, str]] = []
    for order in _authored_orders(request):
        pinned = _resolved_graph_input_schema(run_configuration, order.name)
        if pinned is None:
            return None
        match order.value:
            case WorkItemOrderValue(reference):
                identities.append((order.name, pinned.value, reference.value))
            case ObservedWorkItemOrderValue(revision):
                identities.append((order.name, pinned.value, revision.item.value))
            case InlineOrderValue(content):
                identities.append(
                    _requested_order_identity(order.name, pinned.value, content)
                )
            case ArtifactOrderValue(artifact_hash):
                stored = read_stored_artifact(connection, artifact_hash)
                if stored is None:
                    return None
                identities.append(
                    _requested_order_identity(order.name, pinned.value, stored.content)
                )
            case _ as unreachable:
                assert_never(unreachable)
    return tuple(sorted(identities))


def _requested_orders(orders: tuple[RunInput, ...]) -> tuple[tuple[str, str, str], ...]:
    """The named order set a start asks for, as durable identity reads it.

    Keyed by name rather than by arrival, because the caller's sequence is not
    what the run keeps: `run_inputs_v3` has no position column on purpose, so two
    starts that supply the same orders in different sequences are the same run.
    """
    return tuple(
        sorted(
            _requested_order_identity(
                order.name, order.schema_revision.value, order.value
            )
            for order in orders
        )
    )


class _DurableOrderCorrupt(ValueError):
    """A stored order is not what the only writer of that row could have written.

    Raised rather than answered, because the outer transaction turns a
    `ValueError` into `DurableStateCorrupt`: a store that disagrees with itself
    is not a start to refuse, it is a state to stop on.
    """


def _stored_order_identity(
    name: str, schema_hash: str, value: bytes, value_hash: str
) -> tuple[str, str, str]:
    """One stored order's identity, refusing to read a row that contradicts itself.

    Two things must hold for a row this product wrote: its value hashes to the
    hash beside it, and a value under the work-item schema is the complete
    document `_refused_order` is the only door for. Neither can fail honestly,
    so a failure is durable state that lies -- and deciding "same run" from a
    lie is worse than stopping.
    """

    if value_hash != Sha256Hash.of(value).value:
        raise _DurableOrderCorrupt(
            f"stored order {name!r} does not hash to the hash stored beside it"
        )
    if schema_hash == WORK_ITEM_ORDER_SCHEMA_REVISION.value:
        document = read_work_item_order_document(value)
        if document is None:
            raise _DurableOrderCorrupt(
                f"stored order {name!r} is not the work item document its schema owns"
            )
        return (name, schema_hash, document.reference.value)
    return (name, schema_hash, Sha256Hash.of(value).value)


def _stored_orders(
    connection: Connection, run_id: RunId
) -> tuple[tuple[str, str, str], ...]:
    """The named order set this run already carries, read the same way.

    A stored row is checked against itself first: the value must hash to the
    hash beside it, and a value under the work-item schema must be the complete
    document its only writer produces. Neither can fail for a row this product
    wrote, so a failure is durable state that lies rather than a start to
    refuse -- and answering an identity read off bytes nobody can vouch for
    would decide "same run" from a lie.
    """
    return tuple(
        sorted(
            _stored_order_identity(
                str(record["name"]),
                str(record["schema_revision_hash"]),
                bytes(record["value"]),
                str(record["value_hash"]),
            )
            for record in connection.execute(
                sa.select(run_inputs_v3).where(run_inputs_v3.c.run_id == run_id.value)
            ).mappings()
        )
    )


def _resolved_graph_input_schema(
    run_configuration: RunConfigurationRevision, name: str
) -> PublishedRevisionHash | None:
    """The schema revision the document's own resolution pinned for one order."""
    for resolved in run_configuration.resolutions:
        site = resolved.site
        if site.field == "graph_inputs.schema" and site.entry == name:
            return resolved.revision_hash
    return None


@dataclass(frozen=True)
class _TransactionAgentConfigurationReads:
    """Binding reads through the write transaction's own open connection.

    `resolve_start_bindings` never opens a connection: a start's binding
    decision reads through the exact connection its serialized write
    transaction already holds -- the same locks, the same snapshot -- rather
    than a second read path with its own visibility. The row mappers are the
    same ones `DbosAgentConfigurationCatalog` reads with; only the connection
    differs.
    """

    connection: Connection

    def agent_configuration_revision(
        self, revision_hash: AgentConfigurationRevisionHash
    ) -> tuple[AgentConfigurationRevision, AuthProfileRevision] | None:
        configuration_record = one_record(
            self.connection,
            sa.select(agent_configuration_revisions).where(
                agent_configuration_revisions.c.revision_hash == revision_hash.value
            ),
        )
        if configuration_record is None:
            return None
        configuration = agent_configuration_from_record(configuration_record)
        auth_record = one_record(
            self.connection,
            sa.select(auth_profile_revisions).where(
                auth_profile_revisions.c.revision_hash
                == configuration.auth_profile_revision_hash.value
            ),
        )
        if auth_record is None:
            raise AuthProfileMissingForConfiguration(
                configuration.auth_profile_revision_hash
            )
        return configuration, auth_profile_from_record(auth_record)


@dataclass(frozen=True)
class _ExecutableRevision:
    revision: WorkflowRevision
    graph: WorkflowGraphV3
    resolutions: tuple[ResolvedReference, ...]


@dataclass(frozen=True)
class _BoundStart:
    request: StartPublishedRunRequestV2 | StartPublishedRunRequestV3
    run_configuration: RunConfigurationRevision


def _published_document(
    connection: Connection, revision_hash: WorkflowRevisionHash
) -> bytes | None:
    document = connection.scalar(
        sa.select(workflow_revisions.c.document).where(
            workflow_revisions.c.revision_hash == revision_hash.value
        )
    )
    return None if document is None else bytes(document)


def _stored_identity_differs(
    connection: Connection,
    existing_record: RowMapping,
    request: StartPublishedRunRequestV2 | StartPublishedRunRequestV3,
    graph: WorkflowGraphV3,
    requested_orders: tuple[tuple[str, str, str], ...],
) -> bool:
    return (
        WorkflowFormatVersion(int(existing_record["workflow_format_version"]))
        != graph.format_version
        or str(existing_record["agent_binding_set_hash"])
        != request.agent_bindings.binding_set_hash.value
        or _stored_orders(connection, request.run_id) != requested_orders
    )


def _existing_run_or_unread(
    connection: Connection, bound: _BoundStart, graph: WorkflowGraphV3
) -> DurablePublishedRunResult | None:
    """What an existing run under this id answers, or nothing for a fresh start.

    Unread work items are read by the caller, never inside this write. Authored
    orders are not `run_inputs` yet, so a start carrying them compares after the
    insert -- unless it still names unread items, answered from what was pinned.
    """
    request = bound.request
    existing_record = one_record(
        connection, sa.select(runs).where(runs.c.run_id == request.run_id.value)
    )
    unread = _unread_work_items(request)
    if existing_record is None:
        return DurableWorkItemOrderUnread() if unread else None
    if not unread and _authored_orders(request):
        return None
    requested_orders = (
        _unread_order_identities(connection, request, bound.run_configuration)
        if unread
        else _requested_orders(_supplied_orders(request))
    )
    if requested_orders is None:
        # Not comparable here, and a guess would answer "conflict" for a
        # start the ordinary path can refuse by its own name.
        return DurableWorkItemOrderUnread()
    if str(
        existing_record["revision_hash"]
    ) != request.revision_hash.value or _stored_identity_differs(
        connection, existing_record, request, graph, requested_orders
    ):
        return DurableRunIdentityConflict()
    return DurableRunExisting(
        run_from_record_with_bindings(connection, existing_record)
    )


def _admitted_orders(
    connection: Connection, graph: WorkflowGraphV3, bound: _BoundStart
) -> tuple[RunInput, ...] | DurableV3StartInputRefused:
    """The orders this start carries, pinned and judged before the first row."""
    authored = _authored_orders(bound.request)
    orders = _supplied_orders(bound.request)
    if authored and orders:
        raise RuntimeError("a start names its orders once")
    if authored:
        pinned = _pin_authored_orders(
            connection, graph, bound.run_configuration, authored
        )
        if isinstance(pinned, DurableV3StartInputRefused):
            return pinned
        orders = pinned
    refused = _refused_order(connection, graph, bound.run_configuration, orders)
    return orders if refused is None else refused


def _insert_run(
    connection: Connection, bound: _BoundStart, graph: WorkflowGraphV3, workflow_id: str
) -> int:
    request = bound.request
    connection.execute(
        run_configuration_revisions.insert()
        .prefix_with(_OR_IGNORE)
        .values(
            revision_hash=bound.run_configuration.revision_hash.value,
            preimage=bound.run_configuration.preimage,
        )
    )
    return connection.execute(
        runs.insert()
        .prefix_with(_OR_IGNORE)
        .values(
            run_id=request.run_id.value,
            bootstrap_workflow_id=workflow_id,
            revision_hash=request.revision_hash.value,
            workflow_format_version=graph.format_version,
            agent_binding_set_hash=request.agent_bindings.binding_set_hash.value,
            current_node_id=entry_node_of(graph),
            current_round_ordinal=FIRST_ROUND_ORDINAL,
            state=RunState.STARTED.value,
            state_version=0,
            last_event_sequence=0,
            terminal_hash=None,
            run_configuration_revision_hash=bound.run_configuration.revision_hash.value,
        )
    ).rowcount


def _write_run_members(
    connection: Connection,
    bound: _BoundStart,
    graph: WorkflowGraphV3,
    orders: tuple[RunInput, ...],
) -> None:
    request = bound.request
    binding_set = request.agent_bindings
    if binding_set.bindings:
        connection.execute(
            run_agent_bindings.insert(),
            [
                {
                    "run_id": request.run_id.value,
                    "revision_hash": request.revision_hash.value,
                    "binding_set_hash": binding_set.binding_set_hash.value,
                    "role": binding.role.value,
                    "agent_configuration_revision_hash": (
                        binding.agent_configuration_revision_hash.value
                    ),
                }
                for binding in binding_set.bindings
            ],
        )
    if orders:
        # Written beside the run rather than into it: the same published
        # revision serves every order, so the order belongs to this run and
        # the document belongs to all of them.
        connection.execute(
            run_inputs_v3.insert(),
            [
                {
                    "run_id": request.run_id.value,
                    "name": order.name,
                    "schema_revision_hash": order.schema_revision.value,
                    "value": order.value,
                    "value_hash": order.value_hash.value,
                }
                for order in orders
            ],
        )
    # After the orders, because an order this run carries is a member of the
    # package that binds it -- the content hash a declared reference cannot
    # produce and material can.
    persist_bound_node_executions(
        connection,
        request.run_id,
        WorkflowRevisionHash(request.revision_hash.value),
        graph,
        bound.run_configuration,
        orders,
    )


class DbosDurableRunStarter:
    def __init__(
        self,
        engine: Engine,
        settings: DbosRuntimeSettings,
        agent_executor_registry: AgentExecutorRegistry,
    ) -> None:
        self._engine = engine
        self._settings = settings
        self._agent_executor_registry = agent_executor_registry
        self._published_revisions = DbosCatalogStore(engine)

    def fork_run(self, request: ForkRunRequest) -> DurableRunForkResult:
        """Fork through the same composed runtime dependencies as an ordinary start."""

        from atelier2.adapters.dbos.run_fork_store import DbosRunForkStore

        return DbosRunForkStore(
            self._engine, self._settings, self._agent_executor_registry
        ).fork_run(request)

    def start_published(
        self, request: AnyStartPublishedRunRequest
    ) -> DurablePublishedRunResult:
        """Start one published revision the runtime can execute end to end.

        A V3 revision starts here like any other, because the runtime now drives
        one: the attempt path binds a V3 agent node (#194 H1c), the terminal
        condition belongs to the run rather than a subworkflow node (H1b), and a
        node hands on to the heir its author declared (H2). While none of that
        existed, this seam refused V3 by document family and a separate internal
        foundation seam wrote the run without enqueueing anything; both are gone,
        because a second door is only honest while the first one cannot open.

        What refuses an unexecutable V3 document is one layer down and always
        was: `parse_executable_workflow_document` names what the document still
        waits for -- an uninterpreted node kind, a branch nothing chooses between
        -- before this seam is consulted, so admitting the family here does not
        admit a graph the driver would stall on.
        """
        return self._start(request)

    def _cast_against_model_configuration(
        self,
        connection: Connection,
        request: AnyStartPublishedRunRequest,
        graph: AnyWorkflowDocument,
    ) -> (
        AnyStartPublishedRunRequest
        | DurableInvalidAgentBindings
        | DurableUncastAgentRoles
    ):
        """Resolve every V3 role at the one seam that freezes run bindings."""
        if not isinstance(graph, WorkflowGraphV3):
            return request
        requested = (
            request.agent_bindings
            if isinstance(
                request, (StartPublishedRunRequestV2, StartPublishedRunRequestV3)
            )
            else AgentBindingSet(())
        )
        role_refusal = undeclared_agent_role_refusal(graph, requested)
        if role_refusal is not None:
            return role_refusal
        snapshot = model_configuration_snapshot(connection, self._settings.project_id)
        assert isinstance(snapshot, HostModelConfigurationSnapshot)
        resolved = cast_unbound_roles(
            graph,
            requested,
            snapshot.project_defaults,
            snapshot.registries,
            registered_configurations(
                snapshot.registries,
                requested,
                _TransactionAgentConfigurationReads(connection),
            ),
        )
        if resolved.uncast_roles:
            return DurableUncastAgentRoles(resolved.uncast_roles)
        if isinstance(request, StartPublishedRunRequest):
            # `_start` requires a V2/V3-typed request for a V3 graph even where
            # nothing needed casting -- a bare request declares no
            # `agent_bindings` at all, which a zero-role V3 document (a graph
            # this branch is reached for either way) still must carry.
            return StartPublishedRunRequestV2(
                request.run_id, request.revision_hash, resolved.agent_bindings
            )
        if resolved.agent_bindings == requested:
            return request
        return replace(request, agent_bindings=resolved.agent_bindings)

    def _start(
        self,
        request: AnyStartPublishedRunRequest,
    ) -> DurablePublishedRunResult:
        """Read and judge the revision outside the write, then start under it."""
        client: DBOSClient | None = None
        try:
            with self._engine.connect() as read_connection:
                document = _published_document(read_connection, request.revision_hash)
            if document is None:
                return DurableRunRevisionMissing()
            revision = WorkflowRevision(document)
            if revision.revision_hash != request.revision_hash:
                return DurableStateCorrupt()
            graph = parse_workflow_document(revision.document)
            match evaluate_executability(graph, self._published_revisions):
                case ExecutableDocument(resolutions):
                    read = _ExecutableRevision(revision, graph, resolutions)
                case DocumentNotExecutable():
                    return DurableRunFormatNotExecutable()
                case RegistryUnavailable():
                    return DurableWriteUnavailable()
                case RegistryCorrupt():
                    return DurableStateCorrupt()
                case _ as unreachable:
                    assert_never(unreachable)
            client = DBOSClient(
                system_database_engine=self._engine, use_listen_notify=False
            )
            with canonical_write_transaction(self._engine) as connection:
                return self._start_in_transaction(connection, client, request, read)
        except (OperationalError, PoolTimeoutError):
            return DurableWriteUnavailable()
        except (
            AuthProfileMissingForConfiguration,
            ModelRegistryBytesDisagree,
            ProjectModelDefaultsBytesDisagree,
            ValueError,
            RuntimeError,
            DatabaseError,
        ):
            return DurableStateCorrupt()
        finally:
            if client is not None:
                client.destroy()

    def _start_in_transaction(
        self,
        connection: Connection,
        client: DBOSClient,
        request: AnyStartPublishedRunRequest,
        read: _ExecutableRevision,
    ) -> DurablePublishedRunResult:
        """Refuse a moved revision, bind, answer a retry, resolve, admit, write.

        The role check precedes the retry check, so wrong roles are refused by
        that alone -- the precedence an existing but mismatched run gets too.
        """
        stored_document = _published_document(connection, request.revision_hash)
        if stored_document is None or stored_document != read.revision.document:
            raise RuntimeError(
                "published revision changed between parse and serialized start"
            )
        if WorkflowRevision(stored_document).revision_hash != request.revision_hash:
            raise RuntimeError("published revision bytes disagree with their hash")
        cast = self._cast_against_model_configuration(connection, request, read.graph)
        if isinstance(cast, (DurableInvalidAgentBindings, DurableUncastAgentRoles)):
            return cast
        if not isinstance(
            cast, (StartPublishedRunRequestV2, StartPublishedRunRequestV3)
        ):
            return DurableInvalidAgentBindings()
        bound = _BoundStart(
            cast,
            RunConfigurationRevision(
                WorkflowRevisionHash(read.revision.revision_hash.value),
                cast.agent_bindings.binding_set_hash,
                read.resolutions,
            ),
        )
        role_refusal = agent_role_completeness_refusal(read.graph, cast.agent_bindings)
        if role_refusal is not None:
            return role_refusal
        existing = _existing_run_or_unread(connection, bound, read.graph)
        if existing is not None:
            return existing
        bindings_result = resolve_start_bindings(
            read.graph,
            request.revision_hash,
            cast.agent_bindings,
            _TransactionAgentConfigurationReads(connection),
            self._agent_executor_registry,
        )
        if not isinstance(bindings_result, tuple):
            return bindings_result
        orders = _admitted_orders(connection, read.graph, bound)
        if not isinstance(orders, tuple):
            return orders
        return self._write_run(
            connection, client, bound, read.graph, bindings_result, orders
        )

    def _write_run(
        self,
        connection: Connection,
        client: DBOSClient,
        bound: _BoundStart,
        graph: WorkflowGraphV3,
        resolved_bindings: tuple[ResolvedAgentBinding, ...],
        orders: tuple[RunInput, ...],
    ) -> DurablePublishedRunResult:
        request = bound.request
        workflow_id = bootstrap_workflow_id_for(request.run_id)
        inserted_rows = _insert_run(connection, bound, graph, workflow_id)
        existing_record = one_record(
            connection, sa.select(runs).where(runs.c.run_id == request.run_id.value)
        )
        if existing_record is None:
            raise RuntimeError("inserted run is not readable")
        if inserted_rows == 1:
            record_run_started(connection, request.run_id.value)
        # Built by hand because the binding rows below are not written yet.
        terminal_hash = existing_record["terminal_hash"]
        run = RunV3(
            request.run_id,
            request.revision_hash,
            request.agent_bindings.binding_set_hash,
            resolved_bindings,
            RunState(str(existing_record["state"])),
            str(existing_record["current_node_id"]),
            int(existing_record["state_version"]),
            int(existing_record["last_event_sequence"]),
            bound.run_configuration.revision_hash,
            None if terminal_hash is None else Sha256Hash(str(terminal_hash)),
        )
        if inserted_rows == 0:
            if _stored_identity_differs(
                connection, existing_record, request, graph, _requested_orders(orders)
            ):
                return DurableRunIdentityConflict()
            return DurableRunExisting(run)
        _write_run_members(connection, bound, graph, orders)
        client.enqueue_in_transaction(
            connection,
            {
                "workflow_name": WORKFLOW_NAME,
                "queue_name": QUEUE_NAME,
                "workflow_id": workflow_id,
                "app_version": self._settings.application_version,
            },
            request.run_id.value,
            request.revision_hash.value,
        )
        return DurableRunCreated(run)


class DbosWorkflowRevisionPublisher:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def publish(self, revision: WorkflowRevision) -> DurableRevisionPublicationResult:
        try:
            with canonical_write_transaction(self._engine) as connection:
                inserted = connection.execute(
                    workflow_revisions.insert()
                    .prefix_with(_OR_IGNORE)
                    .values(
                        revision_hash=revision.revision_hash.value,
                        document=revision.document,
                    )
                )
                stored = connection.scalar(
                    sa.select(workflow_revisions.c.document).where(
                        workflow_revisions.c.revision_hash
                        == revision.revision_hash.value
                    )
                )
                if stored is None:
                    raise RuntimeError("inserted workflow revision is not readable")
                durable = WorkflowRevision(bytes(stored))
                if durable.revision_hash != revision.revision_hash:
                    return DurableStateCorrupt()
                if durable.document != revision.document:
                    return DurableRevisionCollision()
                if inserted.rowcount == 1:
                    return DurableRevisionCreated(durable)
                return DurableRevisionExisting(durable)
        except (OperationalError, PoolTimeoutError):
            return DurableWriteUnavailable()
        except (ValueError, RuntimeError, DatabaseError):
            return DurableStateCorrupt()
