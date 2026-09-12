from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from atelier2.contracts.agent_modes import AgentModeMismatch
from atelier2.contracts.agents import AgentBindingSet
from atelier2.contracts.executions import (
    SubmitWaitAnswerRequest,
    WaitAnswerSnapshot,
)
from atelier2.contracts.host_configuration import UncastRole
from atelier2.contracts.node_records_v3 import RunInput
from atelier2.contracts.orders import (
    ArtifactOrderValue,
    InlineOrderValue,
    ObservedWorkItemOrderValue,
    StartOrderValue,
    WorkItemOrderValue,
)
from atelier2.contracts.run_bindings import AnyRun
from atelier2.contracts.runs import RunId, WorkflowRevisionHash
from atelier2.contracts.schemas_v3 import InstanceSchemaViolation


@dataclass(frozen=True)
class DurableWriteUnavailable:
    pass


@dataclass(frozen=True)
class DurableStateCorrupt:
    pass


@dataclass(frozen=True)
class DurableRunCreated:
    run: AnyRun


@dataclass(frozen=True)
class DurableRunExisting:
    run: AnyRun


@dataclass(frozen=True)
class DurableRunRevisionMissing:
    pass


@dataclass(frozen=True)
class DurableRunIdentityConflict:
    pass


@dataclass(frozen=True)
class DurableRunFormatNotExecutable:
    """The named revision is published, and no runtime here executes its format."""


@dataclass(frozen=True)
class DurableInvalidAgentBindings:
    pass


@dataclass(frozen=True)
class DurableUncastAgentRoles:
    roles: tuple[UncastRole, ...]

    def __post_init__(self) -> None:
        if not self.roles or any(
            not isinstance(role, UncastRole) for role in self.roles
        ):
            raise ValueError("an uncast-role refusal must name every uncast role")


@dataclass(frozen=True)
class DurableAgentConfigurationRevisionMissing:
    pass


@dataclass(frozen=True)
class DurableAgentExecutorBindingUnavailable:
    pass


@dataclass(frozen=True)
class DurableAgentExecutorCapabilityUnavailable:
    pass


@dataclass(frozen=True)
class DurableAgentExecutorWithoutWorkspaceFileTools:
    """A node pinned a tool grant, and the executor cast for it touches no file.

    Every capability the closed grant vocabulary carries -- the project's own
    verification, the pushed Atelier commit, the pull request opened from it --
    is about the tree the attempt works in. An executor whose invocation reaches
    no file of that tree can produce no candidate for any of them, so the start
    says so by name instead of paying for an attempt whose only possible answer
    is a report about work nobody did (#1166).
    """

    role: str
    executor_revision: str


@dataclass(frozen=True)
class DurableBindingConstraintRefused:
    """The two nodes named by a `distinct_from` resolved to the same occupation."""

    node: str
    distinct_from: str


@dataclass(frozen=True)
class DurableWorkItemOrderUnread:
    """This start names a work item nobody has read, and no run exists to answer.

    A start with a work-item order arrives twice: once naming the item, and --
    only if no run of that identity exists yet -- once carrying what the caller
    read in between. That order is deliberate. A retry of an existing run must
    answer from what the run already pinned rather than read a moving object
    again, so the durable answer comes first and the reading happens only when
    there is nothing to answer from. Reading inside this seam is not the
    alternative: it would hold a write transaction open across a network call.
    """


type DurablePublishedRunResult = (
    DurableRunCreated
    | DurableRunExisting
    | DurableRunRevisionMissing
    | DurableRunIdentityConflict
    | DurableRunFormatNotExecutable
    | DurableInvalidAgentBindings
    | DurableUncastAgentRoles
    | DurableAgentConfigurationRevisionMissing
    | DurableAgentExecutorBindingUnavailable
    | DurableAgentExecutorCapabilityUnavailable
    | DurableAgentExecutorWithoutWorkspaceFileTools
    | DurableBindingConstraintRefused
    | AgentModeMismatch
    | DurableWriteUnavailable
    | DurableStateCorrupt
    | DurableV3StartInputRefused
    | DurableWorkItemOrderUnread
)


@dataclass(frozen=True)
class StartPublishedRunRequest:
    run_id: RunId
    revision_hash: WorkflowRevisionHash


@dataclass(frozen=True)
class StartPublishedRunRequestV2:
    run_id: RunId
    revision_hash: WorkflowRevisionHash
    agent_bindings: AgentBindingSet


class V3InputRefusal(StrEnum):
    """Every named way one order stops a start before anything is written."""

    MISSING = "missing"
    UNDECLARED = "undeclared"
    DUPLICATED = "duplicated"
    SCHEMA_MISMATCH = "schema-mismatch"
    VALUE_REFUSED = "value-refused"
    UNKNOWN_ARTIFACT = "unknown-artifact"


@dataclass(frozen=True)
class DurableV3StartInputRefused:
    """One order the start cannot honour, named by the input it is about.

    The name comes first because it is what an operator fixes: a refusal that
    said only "schema violated" would send them to read the document to find out
    which order they got wrong.
    """

    name: str
    refusal: V3InputRefusal
    detail: str | None = None
    violation: InstanceSchemaViolation | None = None
    """The field a `VALUE_REFUSED` schema violation is about, when it names one.

    `None` for every other refusal -- an input the graph never declared or never
    supplied is not about a field inside its value -- and for a `VALUE_REFUSED`
    whose earliest violation has no addressable pointer either.
    """


@dataclass(frozen=True)
class AuthoredOrder:
    """An order as a caller supplies it: a name and where its value comes from.

    The document pins the schema. A caller that also named a schema would
    be repeating a decision they do not own; the start looks the pin up.

    The value is inline bytes, the address of a published artifact, or a work
    item -- either one this start has already read, or one it has not. The
    unread form exists so an existing run is answered from what it pinned
    before anything reads a moving object again (`DurableWorkItemOrderUnread`).
    """

    name: str
    value: StartOrderValue

    def __post_init__(self) -> None:
        if self.name == "":
            raise ValueError("an order names a nonempty input")
        if not isinstance(
            self.value,
            (
                InlineOrderValue,
                ArtifactOrderValue,
                ObservedWorkItemOrderValue,
                WorkItemOrderValue,
            ),
        ):
            raise TypeError("an order names where its value comes from")


@dataclass(frozen=True)
class StartPublishedRunRequestV3:
    """A start that carries the orders the document declares, beside it.

    It is a third shape rather than a field on the V2 one because an order only
    exists for a V3 document, and a request that could carry one for a V1 or V2
    run would describe something no graph can read. A V3 document that declares
    no `graph_inputs` still starts through the V2 shape; what refuses a missing
    order is the order itself being absent, not the shape of the request.

    `run_inputs` is the durable form (name, pinned schema, bytes) the first
    door already speaks. `orders` is what a caller can honestly supply: a
    name and the exact bytes. The start pins the schema the document named.
    One start uses one of the two, never both.
    """

    run_id: RunId
    revision_hash: WorkflowRevisionHash
    agent_bindings: AgentBindingSet
    run_inputs: tuple[RunInput, ...] = ()
    orders: tuple[AuthoredOrder, ...] = ()


type AnyStartPublishedRunRequest = (
    StartPublishedRunRequest | StartPublishedRunRequestV2 | StartPublishedRunRequestV3
)


class DurablePublishedRunStarter(Protocol):
    def start_published(
        self, request: AnyStartPublishedRunRequest
    ) -> DurablePublishedRunResult: ...


@dataclass(frozen=True)
class DurableAnswerCreated:
    snapshot: WaitAnswerSnapshot


@dataclass(frozen=True)
class DurableAnswerExisting:
    """The exact answer an idempotent retry found, pending or already applied."""

    snapshot: WaitAnswerSnapshot


@dataclass(frozen=True)
class DurableAnswerRunMissing:
    pass


@dataclass(frozen=True)
class DurableAnswerNodeMissing:
    pass


@dataclass(frozen=True)
class DurableAnswerRevisionConflict:
    pass


@dataclass(frozen=True)
class DurableAnswerStateConflict:
    pass


@dataclass(frozen=True)
class DurableAnswerRoundAnswered:
    """The round named already holds a different answer, pending or applied.

    The same bytes again are `DurableAnswerExisting`; a row the store cannot
    read stays `DurableStateCorrupt`. Only a second answer that contradicts the
    one recorded lands here, and it is a valid state, not a corrupt one.
    """


@dataclass(frozen=True)
class DurableAnswerStale:
    pass


@dataclass(frozen=True)
class DurableAnswerNotAdmitted:
    """The waiting node does not accept these bytes as an answer at all.

    Separate from the state conflict, because the two say different things to
    whoever asked: a state conflict means the run was not waiting for this, and
    this means the run *is* waiting and the value is not one this node's own
    declaration admits. Which declaration refused, and in whose words, stays
    inside the store; what travels is that the submission was never answerable.
    """

    detail: str


type DurableAnswerResult = (
    DurableAnswerCreated
    | DurableAnswerExisting
    | DurableAnswerRunMissing
    | DurableAnswerNodeMissing
    | DurableAnswerRevisionConflict
    | DurableAnswerStateConflict
    | DurableAnswerRoundAnswered
    | DurableAnswerStale
    | DurableAnswerNotAdmitted
    | DurableWriteUnavailable
    | DurableStateCorrupt
)


class TransactionalWaitAnswerer(Protocol):
    def submit_result(
        self, request: SubmitWaitAnswerRequest
    ) -> DurableAnswerResult: ...
