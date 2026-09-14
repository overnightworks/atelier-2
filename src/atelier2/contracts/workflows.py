"""How a completed node hands the run to its successor, or ends it.

The run's continuation rule lives here rather than beside the document
grammar it reads, because both a use case deciding what happened next and
the store committing that decision need it, and neither owns the grammar
(`atelier2.contracts.workflows_v3` does).
"""

from __future__ import annotations

from dataclasses import dataclass

from atelier2.contracts.runs import FIRST_ROUND_ORDINAL, require_exact_round_ordinal
from atelier2.contracts.verdicts import Verdict
from atelier2.contracts.workflows_v3 import (
    LoopVerdictNotRead,
    WorkflowGraphV3,
    is_previous_round_data_edge,
    is_sink_node,
    linear_successor_id,
)


@dataclass(frozen=True)
class RunContinues:
    """A completed node advances the run to this exact successor, in this round.

    The round travels with the successor because they are one decision: the node
    a run moves to and the round it stands in are chosen together, and a caller
    holding only the node would have to guess the other half.
    """

    node_id: str
    round_ordinal: int = FIRST_ROUND_ORDINAL

    def __post_init__(self) -> None:
        if not self.node_id:
            raise ValueError("a run successor node id must not be empty")
        require_exact_round_ordinal(self.round_ordinal)


@dataclass(frozen=True)
class RunCompletes:
    """A completed node is the run's terminal node."""


type NodeCompletion = RunContinues | RunCompletes


def completion_after_node(
    graph: WorkflowGraphV3,
    node_id: str,
    round_ordinal: int = FIRST_ROUND_ORDINAL,
    verdict: Verdict | None = None,
) -> NodeCompletion:
    """Decide continuation once, without inventing a successor for the sink.

    The graph answers both halves through its own `depends_on` edges -- the sink
    rule, and the one declared heir where exactly one exists. Where more than one
    does, it refuses in its own typed words rather than choosing: picking between
    waiting dependents is the ready set #86 owns.

    A declared loop adds the one move the edges cannot express, and adds nothing
    else: the last node of a round hands back to the first. Two things end it,
    and they are read in this order. A verdict the author declared is the early
    exit -- the round's own answer says whether the work goes round again -- and
    the declared bound is the fallback that holds whatever the verdict says. A
    loop that ends either way falls through to the rule above and ends where any
    other node would, with no word invented for it.

    The verdict travels in rather than being read here, because reading it is a
    durable act -- the value a round produced -- and this rule stays a decision
    about the document. A loop that declares one and is asked without it refuses
    by name instead of continuing in whichever direction the omission implies.
    """
    require_exact_round_ordinal(round_ordinal)
    loop = graph.loop_of(node_id)
    if loop is not None and node_id == loop.body[-1]:
        condition = loop.repeat_while
        if condition is not None and verdict is None:
            raise LoopVerdictNotRead(loop.id, node_id)
        round_again = condition is None or verdict is condition.verdict
        if round_again and round_ordinal < loop.maximum_rounds:
            return RunContinues(loop.body[0], round_ordinal + 1)
    if is_sink_node(graph, node_id):
        return RunCompletes()
    successor_id = linear_successor_id(graph, node_id)
    return RunContinues(successor_id, round_of(graph, successor_id, round_ordinal))


def producing_round(
    graph: WorkflowGraphV3,
    reader_id: str,
    source_node: str,
    current_round_ordinal: int,
    producer_last_executed_round: int | None = None,
) -> int | None:
    """Which round wrote the value this reader is asking that source for.

    A source the edges already order ran in this turn of the loop, or once if
    no loop repeats it. A source the same loop repeats that no `depends_on` edge
    can name — the review the next build must read — wrote in the immediately
    previous round. Round one has no previous round, so that input is absent
    rather than refused: honestly empty, not a missing write.

    A source a loop repeats that this reader stands outside of — the loop the
    reader belongs to, if any, is not the source's — wrote its value in the
    round the loop last ran, not in the round the reader itself stands in: the
    run leaves a finished loop and starts again at round one outside it, so
    reading the reader's own round would read round one even after the loop
    turned twice. That round is a durable fact this pure rule does not hold,
    so the caller supplies it as `producer_last_executed_round`; asking for one
    without supplying it is a caller error, not a guess this rule could make.
    """
    require_exact_round_ordinal(current_round_ordinal)
    if is_previous_round_data_edge(graph, reader_id, source_node):
        if current_round_ordinal == FIRST_ROUND_ORDINAL:
            return None
        return current_round_ordinal - 1
    source_loop = graph.loop_of(source_node)
    if source_loop is not None and graph.loop_of(reader_id) != source_loop:
        if producer_last_executed_round is None:
            raise ValueError(
                f"node {reader_id!r} reads {source_node!r} out of loop "
                f"{source_loop.id!r} without its last executed round"
            )
        return producer_last_executed_round
    return round_of(graph, source_node, current_round_ordinal)


def round_of(graph: WorkflowGraphV3, node_id: str, current_round_ordinal: int) -> int:
    """Which round this node stands in while the run is in that round.

    A node inside the loop that is turning stands in the round that is turning;
    a node no loop repeats runs exactly once, whatever the run has been through
    to reach it. One reader of that rule is what keeps a stored execution and the
    execution a later step recomputes from disagreeing.
    """
    if graph.loop_of(node_id) is None:
        return FIRST_ROUND_ORDINAL
    return current_round_ordinal
