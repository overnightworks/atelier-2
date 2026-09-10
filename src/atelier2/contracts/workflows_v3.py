from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Discriminator,
    Field,
    StringConstraints,
    Tag,
    ValidationError,
    field_validator,
    model_validator,
)

from atelier2.contracts.agents import MAXIMUM_SIGNED_INT64
from atelier2.contracts.runs import FIRST_ROUND_ORDINAL
from atelier2.contracts.verdicts import VERDICT_ANSWER_SCHEMA, Verdict
from atelier2.contracts.workflow_refusals import (
    WorkflowDocumentRefused,
    WorkflowRefusal,
    WorkflowRefusalReason,
)

NonemptyString = Annotated[str, StringConstraints(min_length=1)]

MAXIMUM_INSTRUCTION_BYTES = 16 * 1024
MAXIMUM_DOCUMENT_NAME_BYTES = 200
MAXIMUM_DOCUMENT_DESCRIPTION_BYTES = 4 * 1024

type JoinRule = Literal["all_succeeded", "all_terminal"]
type AgentMode = Literal["headless", "headless_with_tools", "interactive"]
type RoleDifficulty = Literal[1, 2, 3]
type RoleKind = Literal["build", "review"]

DEFAULT_SINGLE_DEPENDENCY_JOIN: JoinRule = "all_succeeded"

DEFAULT_ROLE_KIND: RoleKind = "build"
"""What a role that does not say otherwise is there to do.

Judging another's work is the exception a document states, so a silent role
builds. The word says what the work *is*, never how the role is staffed: a
review role is filled by its difficulty like any other.
"""

DEFAULT_ROLE_DIFFICULTY: RoleDifficulty = 2
"""How hard the work of a role that names no difficulty is taken to be.

Three numbers, no names and no rating: the number says how hard the work is,
never which model does it, and which model answers a number is configuration
the operator changes in one place (ADR 0019 §3, sharpening #557's tier ruling).
A document that names none means the middle, so the omission is the ordinary
case rather than an unset field every reader has to interpret for itself.
"""

RETIRED_KEY_REPLACEMENTS: Mapping[str, str] = {
    "start": "depends_on, from which the entry set is derived",
    "next": "depends_on",
    "job": "instruction",
    "output": "outputs",
    "answer_type": "the single declared output and its versioned schema",
    "operands": "inputs",
    "arguments": "inputs",
    "logical_key": "the idempotency key the core derives",
}


def _declared_sequence(value: object) -> object:
    return tuple(value) if type(value) is list else value


DeclaredSequence = BeforeValidator(_declared_sequence)


def _declared_verdict(value: object) -> object:
    """An authored token, read by the vocabulary that owns it.

    A document is written as text, so the token arrives as a string; the closed
    set is where it becomes a verdict or is refused. Reading it here rather than
    restating the tokens as a second literal type is what keeps one owner of the
    vocabulary the engine, the store and the author all speak.
    """
    return Verdict(value) if type(value) is str else value


DeclaredVerdict = BeforeValidator(_declared_verdict)

ExactModelId = Annotated[str, StringConstraints(pattern=r"^\S+$")]
"""One provider model id as written, never an alias and never a sentence.

A pin is sent to a provider as written, so `newest opus` would either be refused
there or resolve to whatever that provider currently means by it -- and the
receipt's two ids, the requested and the confirmed, could no longer be compared.
The shape is part of the published grammar rather than a validator behind it, so
a consumer authoring a document is told the rule before writing one.
"""


def _declared_difficulty(value: object) -> object:
    """One of the three numbers as written, not whatever compares equal to one.

    The language calls `true` equal to 1 and `2.0` equal to 2, and either would
    be accepted as a difficulty the author never wrote -- casting the role at a
    number nobody chose. The written kind decides.
    """
    if type(value) is not int:
        raise ValueError("difficulty is written as the whole number 1, 2 or 3")
    return value


DeclaredDifficulty = BeforeValidator(_declared_difficulty)


def _bounded_authored_text(text: str, subject: str, maximum_bytes: int) -> str:
    if not text.strip():
        raise ValueError(f"{subject} must carry text")
    if len(text.encode("utf-8")) > maximum_bytes:
        raise ValueError(f"{subject} exceeds {maximum_bytes} UTF-8 bytes")
    return text


class _ClosedV3Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class VersionedReference(_ClosedV3Model):
    ref: NonemptyString
    revision: NonemptyString


class RequiredContextSource(_ClosedV3Model):
    ref: NonemptyString
    revision: NonemptyString
    selector: NonemptyString


class RequiredContextEntry(_ClosedV3Model):
    name: NonemptyString
    source: RequiredContextSource


class AvailableContextEntry(_ClosedV3Model):
    name: NonemptyString
    source: VersionedReference
    read_operations: Annotated[
        tuple[VersionedReference, ...], DeclaredSequence, Field(min_length=1)
    ]


class NodeOutputSource(_ClosedV3Model):
    node: NonemptyString
    output: NonemptyString


class NodeReceiptSource(_ClosedV3Model):
    node: NonemptyString
    receipt: Literal["terminal"]


class ContextEntrySource(_ClosedV3Model):
    context: NonemptyString


class GraphInputSource(_ClosedV3Model):
    """One order the graph itself was started with, read by name.

    The other three sources name something the run produces; this one names
    something the run was given. It is therefore the only source available to an
    entry node, and the only one a start has to satisfy before the first node runs.
    """

    graph_input: NonemptyString


def _input_source_form(value: object) -> str | None:
    if not isinstance(value, Mapping):
        return None
    for key in ("output", "receipt", "context", "graph_input"):
        if key in value:
            return key
    return None


InputSource = Annotated[
    Annotated[NodeOutputSource, Tag("output")]
    | Annotated[NodeReceiptSource, Tag("receipt")]
    | Annotated[ContextEntrySource, Tag("context")]
    | Annotated[GraphInputSource, Tag("graph_input")],
    Discriminator(_input_source_form),
]


class NodeInput(_ClosedV3Model):
    name: NonemptyString
    source: InputSource | None = Field(default=None, alias="from")
    value: str | None = None

    @model_validator(mode="after")
    def require_exactly_one_source(self) -> NodeInput:
        if (self.source is None) == (self.value is None):
            raise ValueError(
                f"input {self.name!r} must name exactly one of an upstream source "
                "or an authored value"
            )
        return self


class OutputRefusalDeclaration(_ClosedV3Model):
    """This output may be the product's named agent refusal instead of success.

    The second truth, never a verdict. `reason: required` is the declaration
    that the answer must carry a reason sentence; the contract itself is
    `AGENT_REFUSAL_SCHEMA`.
    """

    reason: Literal["required"]


class NodeOutput(_ClosedV3Model):
    name: NonemptyString
    schema_reference: VersionedReference = Field(alias="schema")
    confirmed_by: Literal["operator"] | None = None
    refusal: OutputRefusalDeclaration | None = None


class GraphInput(_ClosedV3Model):
    name: NonemptyString
    schema_reference: VersionedReference = Field(alias="schema")


class GraphOutput(_ClosedV3Model):
    name: NonemptyString
    source: NodeOutputSource = Field(alias="from")


class IterationGreenCondition(_ClosedV3Model):
    """The child result whose arrival ends the loop, under the revision it agreed.

    It names a result the child declares, never an agent's word for being done.
    """

    output: NonemptyString
    schema_reference: VersionedReference = Field(alias="schema")


class IterationCarry(_ClosedV3Model):
    """One round's result, handed to the next round as part of its order.

    This is a binding the runtime applies between two child runs, deliberately
    not a fourth input source: a source form pointing at the previous round
    would be a document edge onto itself, and the cycle refusal of ADR 0002
    would need an exception it must never have.
    """

    name: NonemptyString
    from_output: NonemptyString
    seed: InputSource


class IterateBlock(_ClosedV3Model):
    """Repeat one child until its declared result says green, at most so often.

    `maximum_rounds` has no default: an unbounded loop must not be reachable by
    omission. Rounds are counted by the atelier itself, so the bound carries no
    meter revision and never becomes a sum across providers (ADR 0013).
    """

    maximum_rounds: int
    until: IterationGreenCondition
    carry: Annotated[tuple[IterationCarry, ...], DeclaredSequence] = ()


class BindingConstraint(_ClosedV3Model):
    """A start-time occupation check between two agent nodes.

    `distinct_from` promises the two nodes resolve to different agent
    configuration revisions. It does not promise independent judgment.
    """

    distinct_from: NonemptyString


class LoopVerdictCondition(_ClosedV3Model):
    """The word that sends this loop around again, and the node that says it.

    It names a verdict the node's own answer carries under the contract this
    product owns -- never prose an agent wrote, and never a value read by
    matching text. The node's declared output pins that contract by revision
    hash, which the document refuses to omit, so what the engine reads back into
    the control flow was already judged before it was read.

    The node is written down rather than derived from the body, because which
    answer ends a round is the one thing a reader of the document should not
    have to know the engine's rule to see. That it must be the node whose
    completion closes the round is what the document refuses when it is not.
    """

    node: NonemptyString
    verdict: Annotated[Verdict, DeclaredVerdict]


class LoopDeclaration(_ClosedV3Model):
    """One stretch of this graph, run again from its head a bounded number of times.

    The loop is the only legal way back. A `depends_on` edge pointing backwards
    stays refused as a cycle, unconditionally, so the graph a reader walks is
    still the acyclic one ADR 0002 decided; what repeats is declared beside the
    edges rather than hidden inside them.

    `maximum_rounds` has no default, and a declared verdict does not soften it.
    An unbounded loop must not be reachable by omission, because a run nobody
    can bound is a run nobody can pay for -- and a loop whose only exit is a
    verdict is a loop the agent producing that verdict could keep alive forever.
    The bound is the fallback that always holds; the verdict is the earlier exit.
    """

    id: NonemptyString
    body: Annotated[tuple[NonemptyString, ...], DeclaredSequence, Field(min_length=1)]
    maximum_rounds: int
    repeat_while: LoopVerdictCondition | None = None


class _NodeV3(_ClosedV3Model):
    id: NonemptyString
    depends_on: Annotated[tuple[NonemptyString, ...], DeclaredSequence] = ()
    join: JoinRule | None = None
    cancellation: VersionedReference | None = None


class AgentNodeV3(_NodeV3):
    """One occurrence of a role, and what this document asks of whoever fills it.

    The role is a neutral name, never a provider and never an agent. Four
    forms, and no others, say what the role is and what filling it should look
    like: `difficulty` is how hard the work is (1, 2 or 3; absent means 2),
    `kind` is whether the work builds or reviews (absent means build),
    `family_differs_from` names the role this one must not share a provider
    family with, and `model` pins one exact provider model id. There is no
    rating and no capability sentence here -- which model answers a difficulty
    is configuration outside the document, and the pin is the one exception a
    document may write.
    """

    type: Literal["agent"]
    role: NonemptyString
    mode: AgentMode
    instruction: NonemptyString
    difficulty: Annotated[RoleDifficulty, DeclaredDifficulty] = DEFAULT_ROLE_DIFFICULTY
    kind: RoleKind = DEFAULT_ROLE_KIND
    family_differs_from: NonemptyString | None = None
    model: ExactModelId | None = None
    profile: VersionedReference | None = None
    skills: Annotated[tuple[VersionedReference, ...], DeclaredSequence] = ()
    tools: Annotated[tuple[VersionedReference, ...], DeclaredSequence] = ()
    policy: VersionedReference | None = None
    required_context: Annotated[tuple[RequiredContextEntry, ...], DeclaredSequence] = ()
    available_context: Annotated[
        tuple[AvailableContextEntry, ...], DeclaredSequence
    ] = ()
    inputs: Annotated[tuple[NodeInput, ...], DeclaredSequence] = ()
    outputs: Annotated[tuple[NodeOutput, ...], DeclaredSequence] = ()
    budget: VersionedReference | None = None
    retry: VersionedReference | None = None
    binding_constraint: BindingConstraint | None = None

    @field_validator("instruction")
    @classmethod
    def bound_authored_instruction(cls, instruction: str) -> str:
        return _bounded_authored_text(
            instruction, "instruction", MAXIMUM_INSTRUCTION_BYTES
        )


class DeterministicNodeV3(_NodeV3):
    type: Literal["deterministic"]
    operation: VersionedReference
    required_context: Annotated[tuple[RequiredContextEntry, ...], DeclaredSequence] = ()
    inputs: Annotated[tuple[NodeInput, ...], DeclaredSequence] = ()
    outputs: Annotated[tuple[NodeOutput, ...], DeclaredSequence, Field(min_length=1)]
    retry: VersionedReference | None = None


class WaitNodeV3(_NodeV3):
    type: Literal["wait"]
    prompt: NonemptyString
    required_context: Annotated[tuple[RequiredContextEntry, ...], DeclaredSequence] = ()
    inputs: Annotated[tuple[NodeInput, ...], DeclaredSequence] = ()
    outputs: Annotated[
        tuple[NodeOutput, ...], DeclaredSequence, Field(min_length=1, max_length=1)
    ]


class SubworkflowNodeV3(_NodeV3):
    type: Literal["subworkflow"]
    workflow: VersionedReference
    inputs: Annotated[tuple[NodeInput, ...], DeclaredSequence] = ()
    outputs: Annotated[tuple[NodeOutput, ...], DeclaredSequence] = ()
    budget: VersionedReference | None = None
    iterate: IterateBlock | None = None


class ActionNodeV3(_NodeV3):
    type: Literal["action"]
    operation: VersionedReference
    required_context: Annotated[tuple[RequiredContextEntry, ...], DeclaredSequence] = ()
    inputs: Annotated[tuple[NodeInput, ...], DeclaredSequence] = ()
    outputs: Annotated[tuple[NodeOutput, ...], DeclaredSequence] = ()


WorkflowNodeV3 = Annotated[
    AgentNodeV3 | DeterministicNodeV3 | WaitNodeV3 | SubworkflowNodeV3 | ActionNodeV3,
    Field(discriminator="type"),
]


class WorkflowGraphV3(_ClosedV3Model):
    format_version: Literal[3]
    name: NonemptyString
    description: NonemptyString | None = None
    graph_inputs: Annotated[tuple[GraphInput, ...], DeclaredSequence] = ()
    graph_outputs: Annotated[tuple[GraphOutput, ...], DeclaredSequence] = ()
    nodes: Annotated[tuple[WorkflowNodeV3, ...], DeclaredSequence, Field(min_length=1)]
    loops: Annotated[tuple[LoopDeclaration, ...], DeclaredSequence] = ()

    @field_validator("name")
    @classmethod
    def bound_one_line_name(cls, name: str) -> str:
        # A label a surface shows on one line must carry no boundary any renderer
        # could break at, so the test is the whole Unicode set the language splits
        # on — CR, VT, FF, the separators, NEL, LS and PS as well as LF.
        if name.splitlines() != [name]:
            raise ValueError("name is the one line a picker shows")
        return _bounded_authored_text(name, "name", MAXIMUM_DOCUMENT_NAME_BYTES)

    @field_validator("description")
    @classmethod
    def bound_description(cls, description: str) -> str:
        return _bounded_authored_text(
            description, "description", MAXIMUM_DOCUMENT_DESCRIPTION_BYTES
        )

    @model_validator(mode="after")
    def validate_vocabulary(self) -> WorkflowGraphV3:
        _refuse_colliding_ids(self.nodes)
        _refuse_broken_control_edges(self.nodes)
        _refuse_unresolvable_order(self.nodes)
        for node in self.nodes:
            _refuse_wrong_join_arity(node)
            _refuse_colliding_declared_names(node)
            _refuse_unbound_inputs(node, self)
            _refuse_broken_iteration(node, self)
            _refuse_unconfirmed_operator_outputs(node)
        _refuse_broken_graph_boundary(self)
        _refuse_broken_loops(self)
        _refuse_broken_binding_constraints(self)
        _refuse_unfillable_role_declarations(self)
        return self

    def node(self, node_id: str) -> WorkflowNodeV3:
        for node in self.nodes:
            if node.id == node_id:
                return node
        raise KeyError(node_id)

    @property
    def entry_node_ids(self) -> tuple[str, ...]:
        return tuple(node.id for node in self.nodes if not node.depends_on)

    @property
    def sink_node_ids(self) -> tuple[str, ...]:
        depended_upon = {
            dependency for node in self.nodes for dependency in node.depends_on
        }
        return tuple(node.id for node in self.nodes if node.id not in depended_upon)

    def join_of(self, node_id: str) -> JoinRule | None:
        """The join a scheduler applies: the omitted single-edge join is explicit."""
        node = self.node(node_id)
        if not node.depends_on:
            return None
        return node.join or DEFAULT_SINGLE_DEPENDENCY_JOIN

    def loop_of(self, node_id: str) -> LoopDeclaration | None:
        """The loop that repeats this node, or None where nothing repeats it.

        One node belongs to at most one loop; two loops claiming it is refused
        while the document is read, so this answer is never a choice.
        """
        self.node(node_id)
        for loop in self.loops:
            if node_id in loop.body:
                return loop
        return None

    def declared_rounds_of(self, node_id: str) -> range:
        """Every round this node may run in, from the document alone.

        A bounded loop is what makes this answerable before anything runs, and
        answerable is the point: a durable record that must exist before a round
        can be receipted has to be writable before the round starts.
        """
        loop = self.loop_of(node_id)
        if loop is None:
            return range(FIRST_ROUND_ORDINAL, FIRST_ROUND_ORDINAL + 1)
        return range(FIRST_ROUND_ORDINAL, FIRST_ROUND_ORDINAL + loop.maximum_rounds)

    def dependency_closure(self, node_id: str) -> frozenset[str]:
        """Every node reachable by following `depends_on` from this node."""
        closure: set[str] = set()
        pending = list(self.node(node_id).depends_on)
        while pending:
            dependency = pending.pop()
            if dependency in closure:
                continue
            closure.add(dependency)
            pending.extend(self.node(dependency).depends_on)
        return frozenset(closure)


def is_previous_round_data_edge(
    graph: WorkflowGraphV3, reader_id: str, source_node: str
) -> bool:
    """Whether this read names a loop-mate the edges cannot order.

    `depends_on` stays acyclic, so the head of a loop cannot name the tail as a
    control predecessor. The value the tail wrote last round is still what the
    next build must read, and that is a data edge onto a previous execution, not
    a cycle in this round.
    """
    reader_loop = graph.loop_of(reader_id)
    source_loop = graph.loop_of(source_node)
    return (
        reader_loop is not None
        and source_loop is not None
        and reader_loop.id == source_loop.id
        and source_node not in graph.dependency_closure(reader_id)
    )


type AnyWorkflowDocument = WorkflowGraphV3
"""Every executable document format the runtime loads.

One name because callers across the durable layer already spell it, not
because more than one format exists behind it (#901 slice 5 retired the V1/V2
document grammar this alias once spanned).
"""

type AnyWorkflowDocumentNode = WorkflowNodeV3


class MultipleSinkCompletionUnsupported(ValueError):
    """V3 needs all-sinks state before one of several sinks can complete a run."""

    def __init__(self, sink_node_ids: tuple[str, ...]) -> None:
        super().__init__(
            "V3 run completion currently requires exactly one sink node, "
            f"not {len(sink_node_ids)}"
        )
        self.sink_node_ids = sink_node_ids


class BranchingAdvanceUnsupported(ValueError):
    """A node's successor is not one declared edge, so no linear rule fits.

    The typed shape exists before the edge it guards: while only a single V3 node
    was startable this could not be reached at all, and a bare `ValueError` there
    would have surfaced through the attempt path as an unhandled error on the day
    it became reachable. Naming it is what lets the caller that opens fan-out --
    the ready set of #86 -- refuse in its own words instead of crashing.
    """

    def __init__(self, node_id: str, dependents: tuple[str, ...]) -> None:
        super().__init__(
            f"node {node_id!r} is followed by {len(dependents)} nodes "
            f"({', '.join(dependents) or 'none'}); advancing past a branch needs "
            "the ready set and its join semantics, which this rule does not decide"
        )
        self.node_id = node_id
        self.dependents = dependents


class LoopVerdictNotRead(ValueError):
    """A loop that a verdict steers was asked to continue without one.

    The document says this node's answer chooses between another round and the
    way out, so continuing without having read it would be choosing an edge by
    default -- silently, and in the expensive direction as often as not. The
    caller that has the answer is the caller that may ask.
    """

    def __init__(self, loop_id: str, node_id: str) -> None:
        super().__init__(
            f"loop {loop_id!r} continues on the verdict of node {node_id!r}, "
            "and none was read"
        )
        self.loop_id = loop_id
        self.node_id = node_id


def _dependents_of(graph: WorkflowGraphV3, node_id: str) -> tuple[str, ...]:
    return tuple(node.id for node in graph.nodes if node_id in node.depends_on)


def is_linear_chain(graph: WorkflowGraphV3) -> bool:
    """Whether the graph is one line, whose advance needs no scheduler.

    Two conditions say that and no more -- one entry, and no node with a second
    dependent. A fan-in needs no clause of its own: reaching two dependencies
    from one entry requires a node somewhere with two dependents, which the
    second condition already refuses. Choosing between waiting dependents is the
    ready set's decision, and this rule declines to make it rather than guessing.
    """

    if any(len(_dependents_of(graph, node.id)) > 1 for node in graph.nodes):
        return False
    entries = [node.id for node in graph.nodes if not node.depends_on]
    return len(entries) == 1


def linear_successor_id(graph: WorkflowGraphV3, node_id: str) -> str:
    """The one node the author declared to run after this one."""

    graph.node(node_id)
    dependents = _dependents_of(graph, node_id)
    if len(dependents) != 1:
        raise BranchingAdvanceUnsupported(node_id, dependents)
    successor = graph.node(dependents[0])
    if len(successor.depends_on) != 1:
        raise BranchingAdvanceUnsupported(successor.id, tuple(successor.depends_on))
    return successor.id


def is_sink_node(graph: WorkflowGraphV3, node_id: str) -> bool:
    """Whether this node's completion completes the run.

    V3 declares dependencies, so its sinks are the nodes nothing depends on;
    more than one is refused in its own typed words rather than guessed at
    (the ready set of #86 owns choosing between them).
    """

    graph.node(node_id)
    sink_node_ids = graph.sink_node_ids
    if len(sink_node_ids) != 1:
        raise MultipleSinkCompletionUnsupported(sink_node_ids)
    return node_id == sink_node_ids[0]


def _refuse(
    reason: WorkflowRefusalReason,
    field: str,
    detail: str,
    node: str | None = None,
) -> WorkflowDocumentRefused:
    return WorkflowDocumentRefused(WorkflowRefusal(reason, field, detail, node))


def _first_duplicate(names: Iterable[str]) -> str | None:
    seen: set[str] = set()
    for name in names:
        if name in seen:
            return name
        seen.add(name)
    return None


def _refuse_colliding_ids(nodes: Sequence[WorkflowNodeV3]) -> None:
    duplicate = _first_duplicate(node.id for node in nodes)
    if duplicate is not None:
        raise _refuse(
            WorkflowRefusalReason.DUPLICATE_NODE_ID,
            "id",
            "two nodes claim the same id",
            duplicate,
        )


def _refuse_broken_control_edges(nodes: Sequence[WorkflowNodeV3]) -> None:
    declared = {node.id for node in nodes}
    for node in nodes:
        duplicate = _first_duplicate(node.depends_on)
        if duplicate is not None:
            raise _refuse(
                WorkflowRefusalReason.DUPLICATE_NAME,
                "depends_on",
                f"dependency {duplicate!r} is declared twice",
                node.id,
            )
        for dependency in node.depends_on:
            if dependency == node.id:
                raise _refuse(
                    WorkflowRefusalReason.CYCLE,
                    "depends_on",
                    "a node may not depend on itself",
                    node.id,
                )
            if dependency not in declared:
                raise _refuse(
                    WorkflowRefusalReason.UNKNOWN_NODE_REFERENCE,
                    "depends_on",
                    f"dependency {dependency!r} names no declared node",
                    node.id,
                )


def _refuse_unresolvable_order(nodes: Sequence[WorkflowNodeV3]) -> None:
    resolved: set[str] = set()
    pending = list(nodes)
    while pending:
        ready = [node for node in pending if resolved.issuperset(node.depends_on)]
        if not ready:
            unresolved = sorted(node.id for node in pending)
            raise _refuse(
                WorkflowRefusalReason.CYCLE,
                "depends_on",
                f"the dependency edges of {', '.join(unresolved)} never release",
                pending[0].id,
            )
        resolved.update(node.id for node in ready)
        ready_ids = {node.id for node in ready}
        pending = [node for node in pending if node.id not in ready_ids]


def _refuse_wrong_join_arity(node: WorkflowNodeV3) -> None:
    if not node.depends_on and node.join is not None:
        raise _refuse(
            WorkflowRefusalReason.JOIN_WITHOUT_DEPENDENCY,
            "join",
            "a node with no dependency has nothing to join",
            node.id,
        )
    if len(node.depends_on) > 1 and node.join is None:
        raise _refuse(
            WorkflowRefusalReason.JOIN_REQUIRED,
            "join",
            "a node with more than one dependency must declare its join",
            node.id,
        )


def _declared_context_names(node: WorkflowNodeV3) -> tuple[str, ...]:
    if isinstance(node, SubworkflowNodeV3):
        return ()
    return tuple(entry.name for entry in node.required_context)


def _refuse_colliding_declared_names(node: WorkflowNodeV3) -> None:
    for field, names in (
        ("inputs", [entry.name for entry in node.inputs]),
        ("outputs", [entry.name for entry in node.outputs]),
        ("required_context", list(_declared_context_names(node))),
        (
            "available_context",
            [entry.name for entry in node.available_context]
            if isinstance(node, AgentNodeV3)
            else [],
        ),
    ):
        duplicate = _first_duplicate(names)
        if duplicate is not None:
            raise _refuse(
                WorkflowRefusalReason.DUPLICATE_NAME,
                field,
                f"{duplicate!r} is declared twice",
                node.id,
            )


def _refuse_unbound_inputs(node: WorkflowNodeV3, graph: WorkflowGraphV3) -> None:
    closure = graph.dependency_closure(node.id)
    for entry in node.inputs:
        if entry.source is not None:
            _refuse_unbound_source(
                node, graph, "inputs", f"input {entry.name!r}", entry.source, closure
            )


def _refuse_unbound_source(
    node: WorkflowNodeV3,
    graph: WorkflowGraphV3,
    field: str,
    subject: str,
    source: InputSource,
    closure: frozenset[str],
) -> None:
    """Hold one declared read to the boundary of the node that declared it.

    Every source form a document may write is bound here, so a reader that
    arrives later — a round's handover seed, say — obeys the rules an input
    already obeys instead of growing a second, weaker owner.
    """
    if isinstance(source, ContextEntrySource):
        if source.context not in _declared_context_names(node):
            raise _refuse(
                WorkflowRefusalReason.UNDECLARED_CONTEXT,
                field,
                f"{subject} reads context {source.context!r}, "
                "which this node does not require",
                node.id,
            )
        return
    if isinstance(source, GraphInputSource):
        if source.graph_input not in {entry.name for entry in graph.graph_inputs}:
            raise _refuse(
                WorkflowRefusalReason.UNDECLARED_GRAPH_INPUT,
                field,
                f"{subject} reads graph input "
                f"{source.graph_input!r}, which this graph does not declare",
                node.id,
            )
        return
    _refuse_unordered_data_edge(node.id, field, subject, source, closure, graph)


def _refuse_unordered_data_edge(
    node_id: str,
    field: str,
    subject: str,
    source: NodeOutputSource | NodeReceiptSource,
    closure: frozenset[str],
    graph: WorkflowGraphV3,
) -> None:
    if source.node not in {node.id for node in graph.nodes}:
        raise _refuse(
            WorkflowRefusalReason.UNKNOWN_NODE_REFERENCE,
            field,
            f"{subject} reads node {source.node!r}, which is not declared",
            node_id,
        )
    if source.node not in closure and not is_previous_round_data_edge(
        graph, node_id, source.node
    ):
        raise _refuse(
            WorkflowRefusalReason.DATA_EDGE_OUTSIDE_CLOSURE,
            field,
            f"{subject} reads node {source.node!r}, which no "
            "depends_on edge orders before this node",
            node_id,
        )
    if isinstance(source, NodeOutputSource):
        _refuse_undeclared_output(
            node_id,
            field,
            source,
            graph,
            f"{subject} reads",
        )


def _refuse_undeclared_output(
    node_id: str | None,
    field: str,
    source: NodeOutputSource,
    graph: WorkflowGraphV3,
    subject: str,
) -> None:
    declared = {output.name for output in graph.node(source.node).outputs}
    if source.output not in declared:
        raise _refuse(
            WorkflowRefusalReason.UNDECLARED_OUTPUT,
            field,
            f"{subject} output {source.output!r}, which node "
            f"{source.node!r} does not declare",
            node_id,
        )


def declared_reads(node: WorkflowNodeV3) -> tuple[InputSource, ...]:
    """Every source this node reads, whichever field declared it.

    A round's handover seed is as much a read of the graph as an input is, so
    the boundary rules count it instead of calling its order unread.
    """
    seeds = (
        tuple(entry.seed for entry in node.iterate.carry)
        if isinstance(node, SubworkflowNodeV3) and node.iterate is not None
        else ()
    )
    return (
        tuple(entry.source for entry in node.inputs if entry.source is not None) + seeds
    )


def _refuse_broken_iteration(node: WorkflowNodeV3, graph: WorkflowGraphV3) -> None:
    """Bound one declared iteration, as far as the document alone can judge it.

    The child's own boundary is not readable here, so what this refuses is what
    the document contradicts by itself. Whether the child declares the result
    the green condition reads is the binding step's refusal, not this one.
    """
    if not isinstance(node, SubworkflowNodeV3) or node.iterate is None:
        return
    iterate = node.iterate
    if not 1 <= iterate.maximum_rounds <= MAXIMUM_SIGNED_INT64:
        raise _refuse(
            WorkflowRefusalReason.UNBOUNDED_ITERATION,
            "maximum_rounds",
            f"{iterate.maximum_rounds} is no round count; a bounded iteration "
            f"declares 1..{MAXIMUM_SIGNED_INT64} rounds",
            node.id,
        )
    for output in node.outputs:
        if (
            output.name == iterate.until.output
            and output.schema_reference != iterate.until.schema_reference
        ):
            raise _refuse(
                WorkflowRefusalReason.ITERATION_GREEN_CONDITION_UNPROVABLE,
                "iterate",
                f"the green condition reads {iterate.until.output!r} under another "
                "schema revision than this node declares for it",
                node.id,
            )
    duplicate = _first_duplicate(entry.name for entry in iterate.carry)
    if duplicate is not None:
        raise _refuse(
            WorkflowRefusalReason.ITERATION_CARRY_UNBOUND,
            "iterate",
            f"{duplicate!r} is carried into the next round twice",
            node.id,
        )
    closure = graph.dependency_closure(node.id)
    for entry in iterate.carry:
        _refuse_unbound_source(
            node, graph, "iterate", f"carry {entry.name!r}", entry.seed, closure
        )


def _refuse_unconfirmed_operator_outputs(node: WorkflowNodeV3) -> None:
    interactive = isinstance(node, AgentNodeV3) and node.mode == "interactive"
    for output in node.outputs:
        if interactive and output.confirmed_by is None:
            raise _refuse(
                WorkflowRefusalReason.UNCONFIRMED_INTERACTIVE_OUTPUT,
                "outputs",
                f"output {output.name!r} of an interactive node must be "
                "confirmed by the operator",
                node.id,
            )
        if not interactive and output.confirmed_by is not None:
            raise _refuse(
                WorkflowRefusalReason.CONFIRMATION_WITHOUT_INTERACTIVE_MODE,
                "outputs",
                f"output {output.name!r} claims operator confirmation, which "
                "only an interactive agent node can carry",
                node.id,
            )


def _refuse_broken_graph_boundary(graph: WorkflowGraphV3) -> None:
    for field, names in (
        ("graph_inputs", [entry.name for entry in graph.graph_inputs]),
        ("graph_outputs", [entry.name for entry in graph.graph_outputs]),
    ):
        duplicate = _first_duplicate(names)
        if duplicate is not None:
            raise _refuse(
                WorkflowRefusalReason.DUPLICATE_NAME,
                field,
                f"{duplicate!r} is declared twice",
            )
    read_graph_inputs = {
        source.graph_input
        for node in graph.nodes
        for source in declared_reads(node)
        if isinstance(source, GraphInputSource)
    }
    for graph_input in graph.graph_inputs:
        if graph_input.name not in read_graph_inputs:
            raise _refuse(
                WorkflowRefusalReason.GRAPH_INPUT_UNREAD,
                "graph_inputs",
                f"graph input {graph_input.name!r} is read by no node, so every "
                "start would have to supply a value nothing consumes",
            )
    declared = {node.id for node in graph.nodes}
    sinks = set(graph.sink_node_ids)
    for entry in graph.graph_outputs:
        if entry.source.node not in declared:
            raise _refuse(
                WorkflowRefusalReason.UNKNOWN_NODE_REFERENCE,
                "graph_outputs",
                f"graph output {entry.name!r} reads node "
                f"{entry.source.node!r}, which is not declared",
            )
        if entry.source.node not in sinks:
            raise _refuse(
                WorkflowRefusalReason.GRAPH_OUTPUT_NOT_SINK,
                "graph_outputs",
                f"graph output {entry.name!r} reads node "
                f"{entry.source.node!r}, which other nodes depend on",
            )
        _refuse_undeclared_output(
            None,
            "graph_outputs",
            entry.source,
            graph,
            f"graph output {entry.name!r} reads",
        )


def _refuse_broken_loops(graph: WorkflowGraphV3) -> None:
    """Hold every declared loop to what the graph alone can judge about it.

    Three things are decided here and nowhere else: that the loop is bounded,
    that no node is claimed by two loops, and that the body is one uninterrupted
    stretch of the order the edges already declare. The third is what keeps the
    construct from becoming a second scheduler: repeating a scattered set of
    nodes would need a rule for what happens between them, and no owner decides
    that.
    """
    duplicate = _first_duplicate(loop.id for loop in graph.loops)
    if duplicate is not None:
        raise _refuse(
            WorkflowRefusalReason.DUPLICATE_NAME,
            "loops",
            f"two loops claim the id {duplicate!r}",
        )
    repeated: dict[str, str] = {}
    for loop in graph.loops:
        if not 1 <= loop.maximum_rounds <= MAXIMUM_SIGNED_INT64:
            raise _refuse(
                WorkflowRefusalReason.UNBOUNDED_ITERATION,
                "maximum_rounds",
                f"{loop.maximum_rounds} is no round count; loop {loop.id!r} "
                f"declares 1..{MAXIMUM_SIGNED_INT64} rounds",
            )
        for member in loop.body:
            if member in repeated:
                raise _refuse(
                    WorkflowRefusalReason.DUPLICATE_NAME,
                    "loops",
                    f"node {member!r} is repeated by loop {repeated[member]!r} "
                    f"and by loop {loop.id!r}",
                    member,
                )
            repeated[member] = loop.id
        _refuse_body_that_is_not_one_line(loop, graph)
        _refuse_a_verdict_the_loop_could_not_read(loop, graph)


def _refuse_body_that_is_not_one_line(
    loop: LoopDeclaration, graph: WorkflowGraphV3
) -> None:
    declared = {node.id for node in graph.nodes}
    for member in loop.body:
        if member not in declared:
            raise _refuse(
                WorkflowRefusalReason.UNKNOWN_NODE_REFERENCE,
                "loops",
                f"loop {loop.id!r} repeats {member!r}, which is not declared",
            )
    body = set(loop.body)
    head, *rest = loop.body
    if set(graph.node(head).depends_on) & body:
        raise _refuse(
            WorkflowRefusalReason.LOOP_BODY_NOT_ONE_LINE,
            "loops",
            f"loop {loop.id!r} enters at {head!r}, which already depends on a "
            "node the same loop repeats",
            head,
        )
    walked = [head]
    for member in rest:
        if tuple(graph.node(member).depends_on) != (walked[-1],):
            raise _refuse(
                WorkflowRefusalReason.LOOP_BODY_NOT_ONE_LINE,
                "loops",
                f"loop {loop.id!r} repeats {member!r} after {walked[-1]!r}, and "
                "no single declared edge orders it there",
                member,
            )
        walked.append(member)
    for node in graph.nodes:
        if node.id in body:
            continue
        entered = set(node.depends_on) & body
        if entered - {loop.body[-1]}:
            raise _refuse(
                WorkflowRefusalReason.LOOP_BODY_NOT_ONE_LINE,
                "loops",
                f"loop {loop.id!r} is left at {min(entered)!r}, which is "
                "not the round's last node",
                node.id,
            )


def _refuse_a_verdict_the_loop_could_not_read(
    loop: LoopDeclaration, graph: WorkflowGraphV3
) -> None:
    """Hold a declared verdict exit to the two things the document alone decides.

    The engine reads a verdict at exactly one moment -- when the node closing a
    round has completed -- so a condition reading any other node names a moment
    that never comes. And it reads the verdict out of a value the schema seam
    has already judged, so the deciding node's one output must pin the contract
    that vocabulary is published under. Both are answerable here, before
    anything is stored, which is what keeps a run from starting towards a
    continuation nobody could decide.
    """
    condition = loop.repeat_while
    if condition is None:
        return
    if condition.node != loop.body[-1]:
        raise _refuse(
            WorkflowRefusalReason.LOOP_VERDICT_NODE_NOT_THE_ROUND_END,
            "loops",
            f"loop {loop.id!r} reads the verdict of {condition.node!r}, and only "
            f"{loop.body[-1]!r} closes its round",
            condition.node,
        )
    pinned = tuple(
        output.schema_reference.revision
        for output in graph.node(condition.node).outputs
    )
    if pinned != (VERDICT_ANSWER_SCHEMA.revision_hash.value,):
        raise _refuse(
            WorkflowRefusalReason.LOOP_VERDICT_UNREADABLE,
            "loops",
            f"loop {loop.id!r} reads the verdict of {condition.node!r}, whose one "
            "output must pin the published verdict contract "
            f"{VERDICT_ANSWER_SCHEMA.revision_hash.value}",
            condition.node,
        )


def _refuse_broken_binding_constraints(graph: WorkflowGraphV3) -> None:
    """Hold `distinct_from` to two agent nodes with different roles.

    The start check compares resolved configuration hashes. A reference that
    names no node, this node, a node without a role, or a node that shares
    this role cannot be a different occupation, so the document refuses it
    before a start can pretend otherwise.
    """

    declared = {node.id: node for node in graph.nodes}
    for node in graph.nodes:
        if not isinstance(node, AgentNodeV3) or node.binding_constraint is None:
            continue
        other_id = node.binding_constraint.distinct_from
        other = declared.get(other_id)
        if other is None:
            raise _refuse(
                WorkflowRefusalReason.UNKNOWN_NODE_REFERENCE,
                "binding_constraint",
                f"distinct_from names {other_id!r}, which is not declared",
                node.id,
            )
        if other_id == node.id:
            raise _refuse(
                WorkflowRefusalReason.INVALID_VALUE,
                "binding_constraint",
                "distinct_from names this node, which cannot be a different occupation",
                node.id,
            )
        if not isinstance(other, AgentNodeV3):
            raise _refuse(
                WorkflowRefusalReason.INVALID_VALUE,
                "binding_constraint",
                f"distinct_from names {other_id!r}, which has no agent binding",
                node.id,
            )
        if other.role == node.role:
            raise _refuse(
                WorkflowRefusalReason.INVALID_VALUE,
                "binding_constraint",
                f"distinct_from names {other_id!r}, which shares role {node.role!r} "
                "and so cannot be a different occupation",
                node.id,
            )


@dataclass(frozen=True, slots=True)
class DeclaredRole:
    """What one document asks of whoever fills one of its roles.

    This is the whole of the document's say in the casting, and it is the seam
    beneath `cast_unbound_roles`: `difficulty` is the number a configured model
    default answers, `model` is the author's own exact pin, and
    `family_differs_from` names the role this one must not share a provider
    family with -- the rule is the workflow's, never an automatism of the house.
    `kind` says what the work is rather than who does it, and stands beside the
    difficulty wherever the role is shown.

    A start override, the project's model defaults and the host's model registry
    all stand above this record; their order stays one decision in
    `cast_unbound_roles` (ADR 0019 §3).
    """

    role: str
    difficulty: RoleDifficulty
    kind: RoleKind
    family_differs_from: str | None
    model: str | None


def declared_roles_of(graph: WorkflowGraphV3) -> tuple[DeclaredRole, ...]:
    """Every role this document declares, once, in the order it first appears.

    The casting resolves per role, not per node, so several nodes carrying one
    role are one declaration. Folding them can never drop a statement, because
    two nodes that author different things for the same role are refused below.
    """
    by_role: dict[str, list[AgentNodeV3]] = {}
    for node in graph.nodes:
        if isinstance(node, AgentNodeV3):
            by_role.setdefault(node.role, []).append(node)
    declared: list[DeclaredRole] = []
    for role, nodes in by_role.items():
        difficulty: RoleDifficulty | None = _agreed(
            nodes, "difficulty", lambda node: node.difficulty
        )
        kind: RoleKind | None = _agreed(nodes, "kind", lambda node: node.kind)
        declared.append(
            DeclaredRole(
                role,
                DEFAULT_ROLE_DIFFICULTY if difficulty is None else difficulty,
                DEFAULT_ROLE_KIND if kind is None else kind,
                _agreed(
                    nodes, "family_differs_from", lambda node: node.family_differs_from
                ),
                _agreed(nodes, "model", lambda node: node.model),
            )
        )
    return tuple(declared)


def _agreed[T](
    nodes: Sequence[AgentNodeV3], field: str, read: Callable[[AgentNodeV3], T]
) -> T | None:
    """The one value this role's nodes authored for `field`, where they authored one.

    Silence is not a statement: a node that names no difficulty inherits what
    the role's other nodes name, and a role nobody gives one takes the default.
    Two authored values are a contradiction, though, and the document refuses it
    rather than obeying a precedence between nodes that nobody wrote down.
    """
    authored = [node for node in nodes if field in node.model_fields_set]
    if not authored:
        return None
    first = authored[0]
    for node in authored[1:]:
        if read(node) != read(first):
            raise _refuse(
                WorkflowRefusalReason.INVALID_VALUE,
                field,
                f"role {node.role!r} declares {read(node)!r} here and "
                f"{read(first)!r} on node {first.id!r}",
                node.id,
            )
    return read(first)


def _refuse_unfillable_role_declarations(graph: WorkflowGraphV3) -> None:
    """Hold what a document asks of a role to something a casting could fill.

    Reading the declarations is the first half: `declared_roles_of` refuses two
    nodes that ask different things of one role, because the casting resolves
    the role once and could only obey one of them.

    The family rule is the second. One that names this node's own role asks for
    a difference from itself, and one that names a role no node declares
    constrains nothing at all -- both would be read as enforced and neither ever
    is, so the document is refused rather than the run pretending it held.
    """
    roles = {role.role for role in declared_roles_of(graph)}
    for node in graph.nodes:
        if not isinstance(node, AgentNodeV3) or node.family_differs_from is None:
            continue
        other_role = node.family_differs_from
        if other_role == node.role:
            raise _refuse(
                WorkflowRefusalReason.INVALID_VALUE,
                "family_differs_from",
                f"role {node.role!r} cannot differ in family from itself",
                node.id,
            )
        if other_role not in roles:
            raise _refuse(
                WorkflowRefusalReason.UNDECLARED_ROLE,
                "family_differs_from",
                f"names role {other_role!r}, which no agent node declares",
                node.id,
            )


def verdict_condition_of(
    graph: AnyWorkflowDocument, node_id: str
) -> LoopVerdictCondition | None:
    """The loop continuation this node's own verdict decides, or None where none is.

    One reader for every executable format, the shape `is_sink_node` already
    gives the terminal rule: only a format-3 document declares a loop, so the
    other formats answer honestly rather than making each caller ask which
    spelling it is holding.
    """
    if not isinstance(graph, WorkflowGraphV3):
        return None
    loop = graph.loop_of(node_id)
    if loop is None or loop.repeat_while is None:
        return None
    return loop.repeat_while if loop.repeat_while.node == node_id else None


_ITERATION_FIELD_REFUSALS: Mapping[str, WorkflowRefusalReason] = {
    "maximum_rounds": WorkflowRefusalReason.UNBOUNDED_ITERATION,
    "seed": WorkflowRefusalReason.ITERATION_CARRY_UNBOUND,
}

_VOCABULARY_FIELDS = frozenset(
    field.alias or name
    for model in (
        WorkflowGraphV3,
        AgentNodeV3,
        DeterministicNodeV3,
        WaitNodeV3,
        SubworkflowNodeV3,
        ActionNodeV3,
        NodeInput,
        NodeOutput,
        OutputRefusalDeclaration,
        GraphInput,
        GraphOutput,
        IterateBlock,
        IterationGreenCondition,
        IterationCarry,
        LoopDeclaration,
        LoopVerdictCondition,
        BindingConstraint,
        RequiredContextEntry,
        AvailableContextEntry,
        VersionedReference,
        RequiredContextSource,
        NodeOutputSource,
        NodeReceiptSource,
        ContextEntrySource,
        GraphInputSource,
    )
    for name, field in model.model_fields.items()
)


def validate_workflow_graph_v3(document: Mapping[str, object]) -> WorkflowGraphV3:
    """Validate one loaded format-3 document into the closed node vocabulary."""
    _refuse_retired_keys(document)
    try:
        return WorkflowGraphV3.model_validate(document, strict=True)
    except ValidationError as error:
        raise _refusal_from(error, document) from error


def _refuse_retired_keys(document: Mapping[str, object]) -> None:
    _refuse_retired_keys_of(document, None)
    nodes = document.get("nodes")
    if not isinstance(nodes, Sequence) or isinstance(nodes, (str, bytes)):
        return
    for node in nodes:
        if isinstance(node, Mapping):
            identifier = node.get("id")
            _refuse_retired_keys_of(
                node, identifier if isinstance(identifier, str) else None
            )


def _refuse_retired_keys_of(mapping: Mapping[str, object], node: str | None) -> None:
    for key in mapping:
        replacement = RETIRED_KEY_REPLACEMENTS.get(key)
        if replacement is not None:
            raise _refuse(
                WorkflowRefusalReason.RETIRED_KEY,
                key,
                f"format 3 retired {key!r}; it is replaced by {replacement}",
                node,
            )


def _refusal_from(
    error: ValidationError, document: Mapping[str, object]
) -> WorkflowDocumentRefused:
    first = error.errors()[0]
    location = first["loc"]
    error_type = str(first["type"])
    raw_context = first.get("ctx")
    context: Mapping[str, object] = (
        raw_context if isinstance(raw_context, Mapping) else {}
    )
    cause = context.get("error")
    detail = str(cause) if cause is not None else str(first["msg"])
    tag_field = _tag_carrying_field(error_type, context)
    if tag_field is None:
        field = _field_name(location)
        reason = _reason_for(error_type, field)
    else:
        field = tag_field
        reason = (
            WorkflowRefusalReason.MISSING_FIELD
            if error_type == "union_tag_not_found"
            else WorkflowRefusalReason.INVALID_VALUE
        )
    return _refuse(reason, field, detail, _node_id_at(location, document))


def _tag_carrying_field(error_type: str, context: Mapping[str, object]) -> str | None:
    """The field whose value chooses the member of a closed union, when one does.

    A node kind is chosen by its own `type` field, so an unknown or absent kind
    is refused there rather than at the collection that holds the node. A
    discriminator that reads the shape of a value instead of one field names no
    field, and its refusal stays where the location points.
    """
    if error_type not in ("union_tag_invalid", "union_tag_not_found"):
        return None
    discriminator = str(context.get("discriminator", ""))
    if len(discriminator) < 3 or not discriminator.startswith("'"):
        return None
    if not discriminator.endswith("'"):
        return None
    return discriminator[1:-1]


def _field_name(location: Sequence[str | int]) -> str:
    for element in reversed(location):
        if isinstance(element, str):
            return element
    return "nodes"


def _reason_for(error_type: str, field: str) -> WorkflowRefusalReason:
    # An iteration field that never parses is refused under the token that names
    # what it costs — an unbounded loop, an unbound handover — rather than under
    # the shape it happened to break, which no author can act on.
    named = _ITERATION_FIELD_REFUSALS.get(field)
    if named is not None:
        return named
    if error_type == "missing":
        return WorkflowRefusalReason.MISSING_FIELD
    if error_type != "extra_forbidden":
        return WorkflowRefusalReason.INVALID_VALUE
    if field in RETIRED_KEY_REPLACEMENTS:
        return WorkflowRefusalReason.RETIRED_KEY
    if field in _VOCABULARY_FIELDS:
        return WorkflowRefusalReason.REFUSED_FIELD
    return WorkflowRefusalReason.UNKNOWN_FIELD


def _node_id_at(
    location: Sequence[str | int], document: Mapping[str, object]
) -> str | None:
    if len(location) < 2 or location[0] != "nodes":
        return None
    index = location[1]
    nodes = document.get("nodes")
    if not isinstance(index, int) or not isinstance(nodes, Sequence):
        return None
    if index >= len(nodes):
        return None
    node = nodes[index]
    if not isinstance(node, Mapping):
        return None
    identifier = node.get("id")
    return identifier if isinstance(identifier, str) else None
