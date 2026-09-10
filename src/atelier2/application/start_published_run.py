from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import assert_never

from atelier2.application.read_work_item_snapshot import (
    WorkItemNotInTracker,
    WorkItemSnapshotRead,
    read_work_item_snapshot,
)
from atelier2.application.refusals import (
    DurableStateCorrupt,
    ProjectSourceNotConnected,
    ReadUnavailable,
    SourcePayloadMalformed,
    WriteUnavailable,
)
from atelier2.contracts.agent_modes import AgentModeMismatch
from atelier2.contracts.agents import (
    AgentBinding,
    AgentBindingSet,
    AgentConfigurationRevisionHash,
    AgentRole,
)
from atelier2.contracts.host_configuration import ProjectId, UncastRole
from atelier2.contracts.orders import (
    ObservedWorkItemOrderValue,
    StartOrderValue,
    WorkItemOrderValue,
)
from atelier2.contracts.run_bindings import AnyRun
from atelier2.contracts.runs import RunId, WorkflowRevisionHash
from atelier2.contracts.schemas_v3 import InstanceSchemaViolation
from atelier2.ports.durable_runs import (
    AnyStartPublishedRunRequest,
    DurableAgentConfigurationRevisionMissing,
    DurableAgentExecutorBindingUnavailable,
    DurableAgentExecutorCapabilityUnavailable,
    DurableAgentExecutorWithoutWorkspaceFileTools,
    DurableBindingConstraintRefused,
    DurableInvalidAgentBindings,
    DurablePublishedRunStarter,
    DurableRunCreated,
    DurableRunExisting,
    DurableRunFormatNotExecutable,
    DurableRunIdentityConflict,
    DurableRunRevisionMissing,
    DurableUncastAgentRoles,
    DurableV3StartInputRefused,
    DurableWorkItemOrderUnread,
    DurableWriteUnavailable,
    StartPublishedRunRequest,
    StartPublishedRunRequestV2,
    StartPublishedRunRequestV3,
    V3InputRefusal,
)
from atelier2.ports.durable_runs import (
    AuthoredOrder as PortAuthoredOrder,
)
from atelier2.ports.durable_runs import (
    DurableStateCorrupt as PortDurableStateCorrupt,
)
from atelier2.ports.issue_observation import TrackerItemSource

_LOG = logging.getLogger("atelier2")


@dataclass(frozen=True)
class RunCreated:
    run: AnyRun


@dataclass(frozen=True)
class RunExisting:
    run: AnyRun


@dataclass(frozen=True)
class RevisionMissing:
    pass


@dataclass(frozen=True)
class RunIdentityConflict:
    pass


@dataclass(frozen=True)
class RunFormatNotExecutable:
    pass


@dataclass(frozen=True)
class InvalidAgentBindings:
    pass


@dataclass(frozen=True)
class UncastAgentRoles:
    roles: tuple[UncastRole, ...]


@dataclass(frozen=True)
class AgentConfigurationRevisionMissing:
    pass


@dataclass(frozen=True)
class AgentExecutorBindingUnavailable:
    pass


@dataclass(frozen=True)
class BindingConstraintRefused:
    """The two nodes named by a `distinct_from` resolved to the same occupation."""

    node: str
    distinct_from: str


@dataclass(frozen=True)
class RunInputRefused:
    """One order this start cannot honour, named by the input it is about."""

    name: str
    refusal: V3InputRefusal
    detail: str | None
    violation: InstanceSchemaViolation | None = None


type StartPublishedRunResult = (
    RunCreated
    | RunExisting
    | RevisionMissing
    | RunIdentityConflict
    | RunFormatNotExecutable
    | InvalidAgentBindings
    | UncastAgentRoles
    | AgentConfigurationRevisionMissing
    | AgentExecutorBindingUnavailable
    | BindingConstraintRefused
    | AgentModeMismatch
    | RunInputRefused
    | WorkItemOrderUnreadable
    | WriteUnavailable
    | DurableStateCorrupt
)


@dataclass(frozen=True)
class AuthoredAgentBinding:
    """One role and the agent-configuration revision an author bound it to."""

    role: str
    agent_configuration_revision_hash: str


@dataclass(frozen=True)
class AuthoredOrder:
    """An order as a caller supplies it: a name and where its value comes from."""

    name: str
    value: StartOrderValue


@dataclass(frozen=True)
class WorkItemOrderUnreadable:
    """The start could not read the tracker item one order names.

    It carries the read's own refusal rather than flattening four different
    answers -- no connection, no such item, an unreachable platform, a payload
    its adapter refused -- into one word a caller cannot act on.
    """

    name: str
    reason: (
        ProjectSourceNotConnected
        | WorkItemNotInTracker
        | SourcePayloadMalformed
        | ReadUnavailable
    )


def start_published_run(
    run_id: RunId,
    revision_hash: WorkflowRevisionHash,
    bindings: tuple[AuthoredAgentBinding, ...] | None,
    starter: DurablePublishedRunStarter,
    orders: tuple[AuthoredOrder, ...] = (),
    project: ProjectId | None = None,
    tracker: TrackerItemSource | None = None,
) -> StartPublishedRunResult:
    """Start one published revision, from the values an author supplied.

    Building the durable request is part of the decision, not a step before it: a
    role or a revision hash that is not one refuses the start, and it refuses it in
    the same vocabulary as everything else that can go wrong here. A caller above
    therefore has one outcome to read rather than an outcome and an exception.

    `bindings` is `None` for a revision that binds no agent, which is a different
    statement from binding an empty set. `orders` is the material the document
    declared as `graph_inputs`; the start pins each one to the schema the
    document named, so a caller does not repeat that hash.

    An order naming a work item is read here, against the served project's
    connected tracker, and becomes the exact observed revision the run pins
    (ADR 0010 §5) -- before any durable row exists, so a start that cannot read
    the item writes nothing.

    **The durable answer comes first, and the reading only if there is none.**
    A start naming a work item asks the store first, carrying the item's name
    rather than its bytes: an existing run answers from what it already pinned,
    so a retry of a run started yesterday neither re-reads a moving object nor
    turns an unreachable tracker into a failed retry. Only when no run of that
    identity exists does this read the item and start again with what it read.
    """
    request = _durable_request(run_id, revision_hash, bindings, _named(orders))
    if request is None:
        return InvalidAgentBindings()
    result = starter.start_published(request)
    if isinstance(result, DurableWorkItemOrderUnread):
        read = _orders_with_work_items_read(orders, project, tracker)
        if isinstance(read, WorkItemOrderUnreadable):
            return read
        request = _durable_request(run_id, revision_hash, bindings, read)
        if request is None:
            return InvalidAgentBindings()
        result = starter.start_published(request)
    match result:
        case DurableRunCreated(run):
            return RunCreated(run)
        case DurableRunExisting(run):
            return RunExisting(run)
        case DurableRunRevisionMissing():
            return RevisionMissing()
        case DurableRunIdentityConflict():
            return RunIdentityConflict()
        case DurableRunFormatNotExecutable():
            return RunFormatNotExecutable()
        case DurableInvalidAgentBindings():
            return InvalidAgentBindings()
        case DurableUncastAgentRoles(roles):
            return UncastAgentRoles(roles)
        case DurableAgentConfigurationRevisionMissing():
            return AgentConfigurationRevisionMissing()
        case (
            DurableAgentExecutorBindingUnavailable()
            | DurableAgentExecutorCapabilityUnavailable()
        ):
            return AgentExecutorBindingUnavailable()
        case DurableAgentExecutorWithoutWorkspaceFileTools() as refused:
            return _executor_without_workspace_file_tools(run_id, refused)
        case DurableBindingConstraintRefused(node, distinct_from):
            return BindingConstraintRefused(node, distinct_from)
        case AgentModeMismatch() as refused:
            return refused
        case DurableV3StartInputRefused(name, refusal, detail, violation):
            return RunInputRefused(name, refusal, detail, violation)
        case DurableWorkItemOrderUnread():
            raise RuntimeError(
                "a start that read its work items was asked to read again"
            )
        case DurableWriteUnavailable():
            return WriteUnavailable()
        case PortDurableStateCorrupt():
            return DurableStateCorrupt()
        case _ as unreachable:
            assert_never(unreachable)


def _executor_without_workspace_file_tools(
    run_id: RunId, refused: DurableAgentExecutorWithoutWorkspaceFileTools
) -> AgentExecutorBindingUnavailable:
    """The answer the executor refusals give, and the cast that must change, logged.

    The same answer as the other two executor refusals, for the same reason an
    asker can act on: the executor this role was cast to cannot serve what its
    node asks for. Which of the three it was is what the answer no longer
    carries, so the operator reading the host's log is told here what the
    durable refusal named -- the cast that has to change, not just that some
    cast did.
    """
    _LOG.warning(
        "Run %s casts role %s onto executor %s, which reaches no file "
        "of the attempt's workspace.",
        run_id.value,
        refused.role,
        refused.executor_revision,
        extra={
            "event": "agent_executor_without_workspace_file_tools",
            "run_id": run_id.value,
            "role": refused.role,
            "executor_revision": refused.executor_revision,
        },
    )
    return AgentExecutorBindingUnavailable()


def _named(orders: tuple[AuthoredOrder, ...]) -> tuple[PortAuthoredOrder, ...]:
    """The same orders, with a work item still named rather than read."""

    return tuple(PortAuthoredOrder(order.name, order.value) for order in orders)


def _orders_with_work_items_read(
    orders: tuple[AuthoredOrder, ...],
    project: ProjectId | None,
    tracker: TrackerItemSource | None,
) -> tuple[PortAuthoredOrder, ...] | WorkItemOrderUnreadable:
    """Turn every work-item order into the exact bytes of one observed revision."""

    read: list[PortAuthoredOrder] = []
    for order in orders:
        if not isinstance(order.value, WorkItemOrderValue):
            read.append(PortAuthoredOrder(order.name, order.value))
            continue
        match read_work_item_snapshot(project, tracker, order.value.reference):
            case WorkItemSnapshotRead(revision=revision):
                # Still typed as a work item all the way to the durable write:
                # the store refuses to keep one under a schema other than the
                # house's, and a retry compares the item it names.
                read.append(
                    PortAuthoredOrder(order.name, ObservedWorkItemOrderValue(revision))
                )
            case (
                ProjectSourceNotConnected()
                | WorkItemNotInTracker()
                | SourcePayloadMalformed()
                | ReadUnavailable()
            ) as refusal:
                return WorkItemOrderUnreadable(order.name, refusal)
            case _ as unreachable:
                assert_never(unreachable)
    return tuple(read)


def _durable_request(
    run_id: RunId,
    revision_hash: WorkflowRevisionHash,
    bindings: tuple[AuthoredAgentBinding, ...] | None,
    orders: tuple[PortAuthoredOrder, ...] = (),
) -> AnyStartPublishedRunRequest | None:
    if bindings is None:
        if orders:
            return None
        return StartPublishedRunRequest(run_id, revision_hash)
    try:
        binding_set = AgentBindingSet(
            tuple(
                AgentBinding(
                    AgentRole(binding.role),
                    AgentConfigurationRevisionHash(
                        binding.agent_configuration_revision_hash
                    ),
                )
                for binding in bindings
            )
        )
        if orders:
            return StartPublishedRunRequestV3(
                run_id,
                revision_hash,
                binding_set,
                orders=orders,
            )
        return StartPublishedRunRequestV2(run_id, revision_hash, binding_set)
    except (TypeError, ValueError):
        return None
