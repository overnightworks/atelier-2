from __future__ import annotations

from atelier2.contracts.workflows_v3 import (
    ActionNodeV3,
    AgentNodeV3,
    ContextEntrySource,
    GraphInputSource,
    NodeInput,
    NodeOutputSource,
    NodeReceiptSource,
    WaitNodeV3,
    WorkflowGraphV3,
    declared_reads,
    is_linear_chain,
)

V3_UNBOUND_AUTHORED_FORMS = (
    "join",
    "profile",
    "skills",
    "policy",
    "retry",
    "cancellation",
    "required_context",
    "available_context",
)
"""Every authored form of an interpreted V3 node that nothing binds at run start.

Naming the forms rather than the kinds is the point. "Only Agent nodes" would
still admit a document declaring skills or a policy, and the run would start
having silently ignored what its author wrote. A document is executable only
when nothing in it is waiting for an owner.

The list is read against whichever forms a node actually declares, so it holds
for the Wait kind too without naming it twice: a Wait node carries `join`,
`cancellation` and `required_context` and no others, and each of the three is
refused on it for the same reason it is refused on an Agent node.

The four role forms left together when `cast_unbound_roles` gained the host
registry and project difficulty lookup. Difficulty, the optional exact model
pin, and the family rule now affect that binding decision; kind is the role's
declared description and needs no separate runtime action.

`inputs` left this list when the start began binding one: it is admitted per
source by `V3_BOUND_INPUT_SOURCES` rather than as a whole form, because only an
order the graph declares has an owner at the start today. `tools` left it the
same way: an attempt now redeems at most one exec-shaped and at most one
effect-shaped grant a node pins, and `MAXIMUM_REDEEMED_TOOL_GRANTS` is where a
third pin is refused rather than silently narrowed. `budget` left it when the
start began binding the pin: the published revision is resolved like a schema
or a tool grant, and the attempt reads the turn bound those bytes named.
"""

MAXIMUM_REDEEMED_TOOL_GRANTS = 2
"""How many tool grants one node may pin: at most one exec-shaped, at most one
effect-shaped, because an attempt redeems one of each shape.

A redemption leaves exactly one receipt per shape per node execution, so a
second grant of the same shape on one node would either go unredeemed or
answer for the first. Which capability a pin names is only known once its
published bytes are read, though, and this reading has no registry to ask --
so the document alone can only refuse a third pin outright at this count. The
finer rule -- the two bound grants must be one of each shape -- is refused
twice over, at the two places a grant's resolved capability is read: first at
start, in `evaluate_executability.py::resolve_document_references`, before a
run exists; again at redemption, in `agent_effect_grants.py`, as the
binding-time invariant a run itself cannot violate even if a start-level
check were ever bypassed.
"""

AGENT_OUTPUT_SHAPE_UNAVAILABLE = "agent-output-shape-unavailable"
"""The name a V3 Agent node is refused under when its output shape has no owner.

The one executable shape is `single-json-output/v1`: exactly one declared output,
whose whole decoded bytes are its value. A node declaring none promises bytes no
schema judges, and a node declaring several promises a runtime that can tell one
of its values from another -- neither has an owner, so both are refused by this
one name rather than by two descriptions of the same missing shape.
"""

AGENT_OUTPUTS_ONE_SHAPE_KEEPS = 1
"""How many outputs `single-json-output/v1` binds, because an attempt writes that many.

An agent attempt completes with one payload, and that payload is the value of the
output its author declared. Zero leaves the bytes unjudged and several leave one
value answered by another; the count is where the document is refused rather than
started under a shape nobody enforces.
"""


V3_BOUND_INPUT_SOURCES = (GraphInputSource, NodeOutputSource)
"""Which `inputs` source a node may read, because the runtime actually binds it.

An order the graph declares is supplied at the start and handed to the node. A
node output is what one producing execution wrote: the same-round predecessor
when `depends_on` orders it, or the immediately previous round when the source
sits in the same loop and the edges cannot name it. The remaining two sources
-- a node's receipt and a context entry -- name something nothing produces yet,
so a document that reads one is refused by the source it named rather than
started and quietly given nothing.
"""


V3_INTERPRETED_NODE_KINDS = (AgentNodeV3, WaitNodeV3, ActionNodeV3)
"""The node classes a run of this build actually reaches and executes.

An Agent node runs its attempt through the durable attempt path; a Wait node
holds the run for a person and is carried on by their answer; a linear Action
node performs the published adapter operation its `operation` pin names. The
remaining two name work no runtime performs, so a document declaring one is
refused by the kind it wrote rather than started and abandoned when the run
arrives there.
"""


def what_a_v3_document_still_waits_for(graph: WorkflowGraphV3) -> str | None:
    """What this document declares that no runtime binds yet, or None if nothing.

    The executable shape is a line of Agent, Wait and linear Action nodes: each
    entered by at most one dependency and followed by at most one dependent,
    ending in a single sink, declaring nothing else optional. It is checked as a
    form rather than as a list of kinds, because a kind check would admit a
    document whose skills or policy the run start then ignores in silence.

    A branch is still refused on purpose. `depends_on` is bound only where it
    names one edge; where a node has several dependents, choosing between them
    is the ready set ADR 0006 hands the scheduler, with fan-out and join
    semantics decided in passing, so this shape stops at the line.
    """
    foreign = sorted(
        {
            node.type
            for node in graph.nodes
            if not isinstance(node, V3_INTERPRETED_NODE_KINDS)
        }
    )
    if foreign:
        return f"node kinds no runtime interprets: {', '.join(foreign)}"
    if not is_linear_chain(graph):
        return (
            f"{len(graph.nodes)} nodes that do not form one line, and choosing "
            "between them needs the ready set no runtime has yet"
        )
    # Authored, not truthy. `skills: []` and `depends_on: []` are things the
    # author wrote, and a run that ignored them would ignore a statement rather
    # than an absence -- so presence in the parsed document decides, and an empty
    # authored form is refused exactly like a filled one.
    declared = sorted(
        {
            form
            for node in graph.nodes
            for form in V3_UNBOUND_AUTHORED_FORMS
            if form in node.model_fields_set
        }
    )
    if declared:
        return f"authored forms nothing binds yet: {', '.join(declared)}"
    unredeemed = _unredeemed_tool_grants(graph)
    if unredeemed is not None:
        return unredeemed
    unbound_output = _unbound_output_forms(graph)
    if unbound_output is not None:
        return unbound_output
    unbound_prompt = _unbound_wait_forms(graph)
    if unbound_prompt is not None:
        return unbound_prompt
    unbound_action = _unbound_action_forms(graph)
    if unbound_action is not None:
        return unbound_action
    unrepeatable = _unrepeatable_loop_forms(graph)
    if unrepeatable is not None:
        return unrepeatable
    return _unbound_input_sources(graph)


def _unrepeatable_loop_forms(graph: WorkflowGraphV3) -> str | None:
    """What a declared loop asks for that no round of this build carries.

    One thing is still nobody's: a value read *out of* a loop names no round. A
    run leaves the loop in whichever round ended it and stands in the first
    round again outside, so the reader would have to say which round wrote the
    value it reads, and no rule here says. A declared verdict decides when a
    loop ends; it does not decide that.

    A round is a second execution of a node. The Agent and Wait kinds both mint
    one: a `WaitNodeBinding` carries the round ordinal it was bound in, and an
    answer is keyed by execution and round, so a repeated Wait now asks its
    question, and reads its answer, under an identity the answer path carries.
    Action still does not repeat -- a repeated effect has no round semantics an
    idempotent write can lean on.
    """
    for loop in graph.loops:
        repeated = sorted(
            {
                graph.node(member).type
                for member in loop.body
                if not isinstance(graph.node(member), (AgentNodeV3, WaitNodeV3))
            }
        )
        if repeated:
            return f"node kinds no round repeats in loop {loop.id!r}: " + ", ".join(
                repeated
            )
    for node in graph.nodes:
        if graph.loop_of(node.id) is not None:
            continue
        for source in declared_reads(node):
            if not isinstance(source, NodeOutputSource):
                continue
            loop = graph.loop_of(source.node)
            if loop is not None:
                return (
                    f"node {node.id!r} reads {source.output!r} of {source.node!r}, "
                    f"which loop {loop.id!r} writes once per round, and no rule "
                    "here names which round it reads"
                )
    return None


def _unbound_wait_forms(graph: WorkflowGraphV3) -> str | None:
    """Which Wait input source the composed question still does not carry.

    Three of its forms are kept. `prompt` is the authored opening, `inputs` from
    a graph order or named predecessor output are composed beneath it, and the
    one declared output is the schema the answer is read against -- the same
    owner that judges every other value this run produces.

    An authored value, receipt or context source remains unbound. Refuse it by
    the source form it wrote rather than accepting a smaller question than the
    document declared.
    """
    for node in graph.nodes:
        if not isinstance(node, WaitNodeV3):
            continue
        unbound = sorted(
            {
                _source_form(entry)
                for entry in node.inputs
                if not isinstance(entry.source, V3_BOUND_INPUT_SOURCES)
            }
        )
        if unbound:
            return (
                f"input sources on wait node {node.id!r} nothing composes into "
                f"its question: {', '.join(unbound)}"
            )
    return None


ACTION_BODY_INPUT_NAME = "body"
"""The one input name a transitive Action reads its whole request body from.

An Action that composes its request from one upstream agent's own output
declares exactly this input, and no other: `graph_action_intent` reads it
through the same output store any other reader uses, ordered by the
dependency closure ADR 0002 already proves rather than by an immediate
`depends_on` edge -- review and a Wait may stand between the two.
"""

DOCUMENTATION_RELEASE_ACTION_INPUT_NAMES = frozenset(
    {"work_item", "candidate", "approved_verdict"}
)
"""The other authored Action input shape this effect path still binds.

A documentation release reads its three graph inputs by name rather than one
upstream node's output -- an independently reviewed order, not a builder's
transitive result -- so it keeps its own closed name set rather than being
read as an unbound `body` line.
"""


def action_body_source(node: ActionNodeV3) -> NodeOutputSource | None:
    """The single upstream output this Action's `body` input names, or None.

    An Action with a bound body input reads exactly one node's output as its
    whole request body; any other input shape -- none, several, a differently
    named entry, or `body` naming anything but a node output -- returns None,
    so the caller refuses it rather than guessing which source was meant.
    """
    if [entry.name for entry in node.inputs] != [ACTION_BODY_INPUT_NAME]:
        return None
    (entry,) = node.inputs
    return entry.source if isinstance(entry.source, NodeOutputSource) else None


def is_documentation_release_action_form(node: ActionNodeV3) -> bool:
    """Whether this Action declares exactly the documentation-release input shape."""
    names = {entry.name for entry in node.inputs}
    return names == DOCUMENTATION_RELEASE_ACTION_INPUT_NAMES and all(
        isinstance(entry.source, GraphInputSource) for entry in node.inputs
    )


def _unbound_action_forms(graph: WorkflowGraphV3) -> str | None:
    """What an authored Action node declares that this effect path does not bind.

    Two authored request shapes are bound: a documentation release's three
    graph inputs, and a transitive line's single `body` input read from one
    upstream agent's own output through the dependency closure that already
    orders it (ADR 0002). Every other declared input shape -- no input
    at all, a receipt or context source, more than one input, or `body` naming
    anything but an Agent's output -- is refused by name, because the old rule
    that an Action's predecessor must itself be an immediate Agent is retired
    with it rather than kept beside it.
    """
    for node in graph.nodes:
        if not isinstance(node, ActionNodeV3):
            continue
        if "outputs" in node.model_fields_set:
            return (
                f"outputs on action node {node.id!r} that nothing hands on; "
                "the effect receipt is the Action's result"
            )
        if is_documentation_release_action_form(node):
            continue
        body_source = action_body_source(node)
        if body_source is None:
            return f"action node {node.id!r} declares no bound input form"
        if not isinstance(graph.node(body_source.node), AgentNodeV3):
            return (
                f"action node {node.id!r} reads {ACTION_BODY_INPUT_NAME!r} from "
                f"{body_source.node!r}, which is not an Agent"
            )
    return None


def _unredeemed_tool_grants(graph: WorkflowGraphV3) -> str | None:
    """What an authored `tools` entry pins that no attempt of this run redeems.

    `tools` left the blanket refusal when an attempt began redeeming the grants a
    node pins: each revision is resolved before the run exists, read as a grant
    where it is pinned, and the redemption leaves its own durable receipt. What a
    grant grants is decided by the published revision rather than here, so the
    only part of the form this pure reading can judge is how many were written
    -- and more than `MAXIMUM_REDEEMED_TOOL_GRANTS` is refused by its count.
    """
    for node in graph.nodes:
        if not isinstance(node, AgentNodeV3):
            continue
        if len(node.tools) > MAXIMUM_REDEEMED_TOOL_GRANTS:
            return (
                f"{len(node.tools)} tool grants on node {node.id!r}, and one "
                f"attempt redeems at most {MAXIMUM_REDEEMED_TOOL_GRANTS}"
            )
    return None


def _unbound_output_forms(graph: WorkflowGraphV3) -> str | None:
    """What an authored `outputs` entry declares that nothing here keeps.

    `outputs` left the blanket refusal when the runtime began handing a node's
    value to the node that reads it, and admitting the form means keeping what it
    says. Two parts of it are still nobody's:

    * `confirmed_by` promises a human confirmed the artifact, and nothing asks
      anyone;
    * an output count other than one promises a shape `single-json-output/v1` is
      not, and both directions are refused under `AGENT_OUTPUT_SHAPE_UNAVAILABLE`.

    The part that is kept is the value itself: the run writes it durably when the
    producing node completes, hash-bound, and reads it against the schema its
    author pinned -- before the success is written and again before it is handed
    on. Join, retry, canary, and the operator confirmation refused here are
    still owned elsewhere.
    """
    if graph.graph_outputs:
        return "graph outputs nothing carries out of a run: " + ", ".join(
            sorted(entry.name for entry in graph.graph_outputs)
        )
    for node in graph.nodes:
        if not isinstance(node, AgentNodeV3):
            continue
        if len(node.outputs) != AGENT_OUTPUTS_ONE_SHAPE_KEEPS:
            return (
                f"{AGENT_OUTPUT_SHAPE_UNAVAILABLE}: {len(node.outputs)} outputs on "
                f"node {node.id!r}, and an agent node completes with the one value "
                "its own schema judges"
            )
        if any(output.confirmed_by is not None for output in node.outputs):
            return "an output confirmed by an operator nothing asks"
    return None


def _unbound_input_sources(graph: WorkflowGraphV3) -> str | None:
    """Which `inputs` source this document reads that the start cannot supply.

    An authored `value` is refused with the rest: it is a constant inside the
    document, which is the very thing an order replaces, and nothing hands it to
    a node today either.
    """
    unbound = sorted(
        {
            _source_form(entry)
            for node in graph.nodes
            if isinstance(node, AgentNodeV3)
            for entry in node.inputs
            if not isinstance(entry.source, V3_BOUND_INPUT_SOURCES)
        }
    )
    if unbound:
        return f"input sources nothing binds yet: {', '.join(unbound)}"
    return None


def _source_form(entry: NodeInput) -> str:
    """What an author wrote as this input's source, named back to them."""
    if entry.source is None:
        return "value"
    if isinstance(entry.source, NodeOutputSource):
        return "output"
    if isinstance(entry.source, NodeReceiptSource):
        return "receipt"
    if isinstance(entry.source, ContextEntrySource):
        return "context"
    return "graph_input"
