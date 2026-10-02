from __future__ import annotations

import pytest

from atelier2.adapters.yaml_workflows import (
    WorkflowFormatNotExecutable,
    parse_executable_workflow_document,
    parse_workflow_document,
)
from atelier2.contracts.runs import FIRST_ROUND_ORDINAL
from atelier2.contracts.verdicts import VERDICT_ANSWER_SCHEMA, Verdict
from atelier2.contracts.workflows import (
    RunCompletes,
    RunContinues,
    completion_after_node,
    producing_round,
)
from atelier2.contracts.workflows_v3 import (
    BranchingAdvanceUnsupported,
    LoopVerdictNotRead,
    WorkflowGraphV3,
)

_ANY_JSON_REVISION = "e" * 64
_DECLARED_OUTPUT = f"""    outputs:
      - name: result
        schema: {{ref: any-json, revision: "{_ANY_JSON_REVISION}"}}
""".encode()
"""The one output every executable agent node declares, as `single-json-output/v1`.

These documents are parsed, never run, so the revision it pins is a placeholder
of the right shape: what is under test is the form the executable admission
requires, not the schema a run would resolve.
"""

_ONE_NODE = (
    b"""format_version: 3
name: One agent
nodes:
  - id: implement
    type: agent
    role: builder
    mode: headless
    instruction: Do the one thing.
"""
    + _DECLARED_OUTPUT
)

_LINE = (
    _ONE_NODE
    + b"""  - id: review
    type: agent
    role: reviewer
    mode: headless
    instruction: Judge the first thing.
    depends_on: [implement]
"""
    + _DECLARED_OUTPUT
)

_ACTION_LINE = (
    _ONE_NODE
    + f"""  - id: publish
    type: action
    operation: {{ref: open-pr, revision: "{_ANY_JSON_REVISION}"}}
    depends_on: [implement]
    inputs:
      - name: body
        from: {{node: implement, output: result}}
""".encode()
)

_FAN_OUT = (
    _LINE
    + b"""  - id: document
    type: agent
    role: writer
    mode: headless
    instruction: Write it up.
    depends_on: [implement]
"""
    + _DECLARED_OUTPUT
)

_DIAMOND = (
    _FAN_OUT
    + b"""  - id: ship
    type: agent
    role: shipper
    mode: headless
    instruction: Ship it.
    depends_on: [review, document]
    join: all_succeeded
"""
    + _DECLARED_OUTPUT
)

_FAN_IN = (
    _ONE_NODE
    + b"""  - id: research
    type: agent
    role: researcher
    mode: headless
    instruction: Do the other first thing.
"""
    + _DECLARED_OUTPUT
    + b"""  - id: review
    type: agent
    role: reviewer
    mode: headless
    instruction: Judge both.
    depends_on: [implement, research]
    join: all_succeeded
"""
    + _DECLARED_OUTPUT
)


_LOOPED_LINE = (
    _LINE
    + b"""loops:
  - id: until_reviewed
    body: [implement, review]
    maximum_rounds: 3
"""
)

_VERDICT_OUTPUT = f"""    outputs:
      - name: verdict
        schema:
          ref: node-verdict
          revision: "{VERDICT_ANSWER_SCHEMA.revision_hash.value}"
""".encode()
"""The output a node must declare to have its answer read as a verdict.

Taken from the contract rather than written out, because what a document has to
pin is the product's decision; a copy here would survive that decision moving.
"""

_VERDICT_STEERED_LOOP = (
    _ONE_NODE
    + b"""  - id: review
    type: agent
    role: reviewer
    mode: headless
    instruction: Judge the first thing.
    depends_on: [implement]
"""
    + _VERDICT_OUTPUT
    + b"""loops:
  - id: until_reviewed
    body: [implement, review]
    maximum_rounds: 3
    repeat_while: {node: review, verdict: revise}
"""
)

_LOOP_BEFORE_A_TAIL = (
    _LINE
    + b"""  - id: ship
    type: agent
    role: shipper
    mode: headless
    instruction: Ship what came out.
    depends_on: [review]
"""
    + _DECLARED_OUTPUT
    + b"""loops:
  - id: until_reviewed
    body: [implement, review]
    maximum_rounds: 2
"""
)


_LOOPED_WAIT = (
    _ONE_NODE
    + b"""  - id: approve
    type: wait
    prompt: Is this good enough?
    depends_on: [implement]
"""
    + _DECLARED_OUTPUT
    + b"""loops:
  - id: until_approved
    body: [implement, approve]
    maximum_rounds: 2
"""
)
"""#658: a Wait's round now carries an identity the answer path keeps, so this
shape is executable -- see `test_a_loop_may_repeat_its_wait_node`.
"""

_LOOPED_ACTION = (
    _ACTION_LINE
    + b"""loops:
  - id: until_published
    body: [implement, publish]
    maximum_rounds: 2
"""
)
"""What `_LOOPED_WAIT` used to be: a kind that still has no round semantics an
idempotent write can lean on, so a loop repeating it stays refused."""

_LOOP_HEAD_READS_PREVIOUS_REVIEW = (
    b"""format_version: 3
name: Build, then review, the builder reading the last review
nodes:
  - id: implement
    type: agent
    role: builder
    mode: headless
    instruction: Do the one thing.
    inputs:
      - name: last_review
        from: {node: review, output: verdict}
"""
    + _DECLARED_OUTPUT
    + b"""  - id: review
    type: agent
    role: reviewer
    mode: headless
    instruction: Judge the first thing.
    depends_on: [implement]
"""
    + _VERDICT_OUTPUT
    + b"""loops:
  - id: until_reviewed
    body: [implement, review]
    maximum_rounds: 3
    repeat_while: {node: review, verdict: revise}
"""
)


_READ_OUT_OF_A_LOOP = (
    _LINE
    + b"""  - id: ship
    type: agent
    role: shipper
    mode: headless
    instruction: Ship what came out.
    depends_on: [review]
    inputs:
      - name: verdict
        from: {node: review, output: result}
"""
    + _DECLARED_OUTPUT
    + b"""loops:
  - id: until_reviewed
    body: [implement, review]
    maximum_rounds: 2
"""
)


def _graph(document: bytes) -> WorkflowGraphV3:
    graph = parse_workflow_document(document)
    assert isinstance(graph, WorkflowGraphV3)
    return graph


@pytest.mark.proves("a-completed-v3-attempt-reaches-the-runs-terminal-hash")
def test_after_a_v3_node_its_one_declared_heir_is_next() -> None:
    graph = _graph(_LINE)

    assert completion_after_node(graph, "implement") == RunContinues("review")
    assert completion_after_node(graph, "review") == RunCompletes()


@pytest.mark.proves("every-v3-shape-no-runtime-binds-is-refused-by-name")
def test_a_line_of_agent_nodes_is_executable() -> None:
    graph = parse_executable_workflow_document(_LINE)

    assert isinstance(graph, WorkflowGraphV3)
    assert graph.sink_node_ids == ("review",)


@pytest.mark.proves(
    "an-open-pr-adapter-operation-is-published-and-pinned-by-a-v3-action"
)
def test_a_line_of_agent_then_action_is_executable() -> None:
    graph = parse_executable_workflow_document(_ACTION_LINE)

    assert isinstance(graph, WorkflowGraphV3)
    assert graph.sink_node_ids == ("publish",)
    assert completion_after_node(graph, "implement") == RunContinues("publish")
    assert completion_after_node(graph, "publish") == RunCompletes()


@pytest.mark.proves("every-v3-shape-no-runtime-binds-is-refused-by-name")
@pytest.mark.parametrize("document", [_FAN_OUT, _FAN_IN, _DIAMOND])
def test_a_branching_v3_document_stays_refused_for_the_ready_set(
    document: bytes,
) -> None:
    with pytest.raises(WorkflowFormatNotExecutable) as refused:
        parse_executable_workflow_document(document)

    assert "ready set" in str(refused.value)


@pytest.mark.proves("a-completed-v3-attempt-reaches-the-runs-terminal-hash")
def test_a_branching_v3_graph_never_invents_a_single_successor() -> None:
    # The diamond has one entry and one sink, so nothing else refuses first:
    # what must refuse is the advance rule itself, naming the branch.
    with pytest.raises(BranchingAdvanceUnsupported) as refused:
        completion_after_node(_graph(_DIAMOND), "implement")

    assert refused.value.node_id == "implement"
    assert set(refused.value.dependents) == {"review", "document"}


@pytest.mark.proves("a-completed-v3-attempt-reaches-the-runs-terminal-hash")
def test_a_node_is_never_advanced_into_before_its_other_dependency_ran() -> None:
    # `research` has exactly one dependent, so the branch check upstream never
    # fires -- but that heir also waits on `implement`. Advancing there would
    # start a node whose other dependency has not run, which is the join the
    # ready set owns.
    with pytest.raises(BranchingAdvanceUnsupported) as refused:
        completion_after_node(_graph(_FAN_IN), "research")

    assert refused.value.node_id == "review"
    assert set(refused.value.dependents) == {"implement", "research"}


@pytest.mark.proves("a-declared-loop-runs-its-rounds-and-ends-at-its-bound")
def test_the_last_node_of_a_round_hands_back_to_the_first_while_rounds_remain() -> None:
    graph = _graph(_LOOPED_LINE)

    assert completion_after_node(graph, "review", 1) == RunContinues("implement", 2)
    assert completion_after_node(graph, "review", 2) == RunContinues("implement", 3)


@pytest.mark.proves("a-declared-loop-runs-its-rounds-and-ends-at-its-bound")
def test_inside_a_round_the_advance_is_the_line_it_always_was() -> None:
    graph = _graph(_LOOPED_LINE)

    assert completion_after_node(graph, "implement", 2) == RunContinues("review", 2)


@pytest.mark.proves("a-declared-loop-runs-its-rounds-and-ends-at-its-bound")
def test_a_loop_that_declares_no_verdict_ends_at_its_bound_and_nowhere_else() -> None:
    """Nothing this round produced is asked: the round count is the whole rule."""
    graph = _graph(_LOOPED_LINE)

    assert completion_after_node(graph, "review", 3) == RunCompletes()


@pytest.mark.proves("a-declared-verdict-ends-a-loop-before-its-bound")
def test_the_declared_verdict_sends_the_loop_round_again() -> None:
    graph = _graph(_VERDICT_STEERED_LOOP)

    assert completion_after_node(graph, "review", 1, Verdict.REVISE) == RunContinues(
        "implement", 2
    )


@pytest.mark.proves("a-declared-verdict-ends-a-loop-before-its-bound")
def test_any_other_verdict_leaves_the_loop_with_rounds_to_spare() -> None:
    """The early exit is the point: two rounds are left, and none of them runs."""
    graph = _graph(_VERDICT_STEERED_LOOP)

    assert completion_after_node(graph, "review", 1, Verdict.ACCEPTED) == RunCompletes()


@pytest.mark.proves("a-declared-verdict-ends-a-loop-before-its-bound")
def test_the_declared_bound_still_ends_a_loop_whose_verdict_keeps_asking() -> None:
    """A verdict is the earlier exit, never a way past the bound."""
    graph = _graph(_VERDICT_STEERED_LOOP)

    assert completion_after_node(graph, "review", 3, Verdict.REVISE) == RunCompletes()


@pytest.mark.proves(
    "the-continuation-a-verdict-steers-is-read-from-what-the-round-kept"
)
def test_a_loop_a_verdict_steers_is_never_continued_without_one() -> None:
    """Only the node whose answer decides is asked for one, and it is asked."""
    graph = _graph(_VERDICT_STEERED_LOOP)

    assert completion_after_node(graph, "implement", 2) == RunContinues("review", 2)
    with pytest.raises(LoopVerdictNotRead) as refused:
        completion_after_node(graph, "review", 1)

    assert (refused.value.loop_id, refused.value.node_id) == (
        "until_reviewed",
        "review",
    )


@pytest.mark.proves("a-declared-loop-runs-its-rounds-and-ends-at-its-bound")
def test_a_node_after_a_loop_is_entered_in_the_first_round_it_has() -> None:
    """Leaving the loop leaves its rounds behind rather than carrying a count on."""
    graph = _graph(_LOOP_BEFORE_A_TAIL)

    assert completion_after_node(graph, "review", 2) == RunContinues(
        "ship", FIRST_ROUND_ORDINAL
    )
    assert completion_after_node(graph, "ship") == RunCompletes()


@pytest.mark.proves("every-v3-shape-no-runtime-binds-is-refused-by-name")
def test_a_line_a_loop_repeats_is_executable() -> None:
    graph = parse_executable_workflow_document(_LOOPED_LINE)

    assert isinstance(graph, WorkflowGraphV3)
    assert graph.declared_rounds_of("implement") == range(1, 4)
    assert graph.declared_rounds_of("review") == range(1, 4)


@pytest.mark.proves("a-later-round-reads-the-previous-rounds-output")
def test_a_loop_head_may_read_the_loop_tails_previous_round() -> None:
    """The cycle ADR 0002 refuses is a control edge. This is not one."""
    graph = parse_executable_workflow_document(_LOOP_HEAD_READS_PREVIOUS_REVIEW)

    assert isinstance(graph, WorkflowGraphV3)
    last_review = graph.node("implement").inputs[0]
    assert last_review.name == "last_review"
    assert producing_round(graph, "implement", "review", FIRST_ROUND_ORDINAL) is None
    assert producing_round(graph, "implement", "review", 2) == 1
    assert producing_round(graph, "review", "implement", 2) == 2


@pytest.mark.proves("every-v3-shape-no-runtime-binds-is-refused-by-name")
def test_a_loop_shape_no_round_of_this_build_carries_is_refused_by_name() -> None:
    """An action node inside a loop body would run: what it lacks is an owner
    for the round its repeated effect would need."""
    with pytest.raises(WorkflowFormatNotExecutable) as refused:
        parse_executable_workflow_document(_LOOPED_ACTION)

    assert "no round repeats" in str(refused.value)


def test_a_node_after_a_loop_may_read_what_the_loop_produced() -> None:
    """The shape that used to be refused for naming no round is executable now:
    a reader outside the loop reads the round the loop last actually ran.
    """
    graph = parse_executable_workflow_document(_READ_OUT_OF_A_LOOP)

    assert isinstance(graph, WorkflowGraphV3)
    assert graph.sink_node_ids == ("ship",)


def test_a_reader_outside_a_loop_reads_the_round_its_source_last_executed() -> None:
    graph = _graph(_READ_OUT_OF_A_LOOP)

    assert producing_round(graph, "ship", "review", FIRST_ROUND_ORDINAL, 2) == 2


def test_a_reader_outside_a_loop_refuses_without_its_sources_last_round() -> None:
    """The last executed round is a durable fact this pure rule cannot guess."""
    graph = _graph(_READ_OUT_OF_A_LOOP)

    with pytest.raises(ValueError, match="last executed round"):
        producing_round(graph, "ship", "review", FIRST_ROUND_ORDINAL)


def test_a_loop_may_repeat_its_wait_node() -> None:
    """#658: a Wait's round now carries an identity the answer path keeps.

    The document that used to be refused for exactly this shape now parses as
    executable, and the round bound it declares is answerable for the Wait
    exactly as it already was for the Agent beside it.
    """
    graph = parse_executable_workflow_document(_LOOPED_WAIT)

    assert isinstance(graph, WorkflowGraphV3)
    assert graph.declared_rounds_of("implement") == range(1, 3)
    assert graph.declared_rounds_of("approve") == range(1, 3)
