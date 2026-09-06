from __future__ import annotations

from collections.abc import MutableSequence
from pathlib import Path
from typing import cast

import pytest

from atelier2.adapters.yaml_workflows import (
    FORMAT_V3_NOT_EXECUTABLE,
    MAXIMUM_DOCUMENT_DEPTH,
    InvalidWorkflowDocument,
    parse_executable_workflow_document,
    parse_workflow_document,
)
from atelier2.contracts.runs import WorkflowRevision
from atelier2.contracts.verdicts import VERDICT_ANSWER_SCHEMA, Verdict
from atelier2.contracts.workflow_refusals import WorkflowRefusal, WorkflowRefusalReason
from atelier2.contracts.workflows_v3 import (
    AGENT_OUTPUT_SHAPE_UNAVAILABLE,
    DEFAULT_ROLE_DIFFICULTY,
    DEFAULT_ROLE_KIND,
    MAXIMUM_DOCUMENT_DESCRIPTION_BYTES,
    MAXIMUM_DOCUMENT_NAME_BYTES,
    MAXIMUM_INSTRUCTION_BYTES,
    RETIRED_KEY_REPLACEMENTS,
    ActionNodeV3,
    AgentNodeV3,
    ContextEntrySource,
    DeclaredRole,
    DeterministicNodeV3,
    GraphInputSource,
    NodeOutputSource,
    NodeReceiptSource,
    SubworkflowNodeV3,
    WaitNodeV3,
    WorkflowGraphV3,
    declared_roles_of,
    verdict_condition_of,
)

DOCUMENT_NAME = "Self-build review chain"
NAME_LINE = f"name: {DOCUMENT_NAME}"

DOCUMENT = b"""format_version: 3
name: Self-build review chain
graph_inputs:
  - name: brief
    schema: {ref: work_brief, revision: schema-brief}
graph_outputs:
  - name: landed
    from: {node: publish_report, output: receipt}
nodes:
  - id: implement
    type: agent
    role: builder
    mode: headless
    instruction: |
      Implement every acceptance sentence of the bound story.
    profile: {ref: builder_method, revision: profile-1}
    skills:
      - {ref: workspace_discipline, revision: skill-1}
    tools:
      - {ref: repository_write, revision: grant-1}
    policy: {ref: house_rules, revision: policy-1}
    budget: {ref: build_budget, revision: budget-1}
    retry: {ref: twice, revision: retry-1}
    cancellation: {ref: drain, revision: cancel-1}
    required_context:
      - name: story
        source: {ref: requirement, revision: requirement-1, selector: story_acceptance}
    available_context:
      - name: decision_records
        source: {ref: decision_record_index, revision: index-1}
        read_operations:
          - {ref: search, revision: read-1}
    inputs:
      - name: story_text
        from: {context: story}
      - name: order
        from: {graph_input: brief}
      - name: label
        value: needs-review
    outputs:
      - name: candidate
        schema: {ref: workspace_candidate, revision: schema-candidate}
  - id: code_review
    type: agent
    role: reviewer
    mode: headless
    instruction: Name every defect with the sentence it violates.
    depends_on: [implement]
    inputs:
      - name: candidate
        from: {node: implement, output: candidate}
    outputs:
      - name: findings
        schema: {ref: review_verdict, revision: schema-verdict}
  - id: rehearse
    type: subworkflow
    depends_on: [implement]
    workflow: {ref: review_panel, revision: workflow-1}
    budget: {ref: child_budget, revision: budget-2}
    inputs:
      - name: candidate
        from: {node: implement, output: candidate}
    outputs:
      - name: rehearsal
        schema: {ref: review_verdict, revision: schema-verdict}
  - id: approve
    type: wait
    depends_on: [code_review]
    prompt: Approve this candidate, or reject it naming the blocking defect.
    required_context:
      - name: story
        source: {ref: requirement, revision: requirement-1, selector: story_acceptance}
    inputs:
      - name: findings
        from: {node: code_review, output: findings}
    outputs:
      - name: approval
        schema: {ref: landing_approval, revision: schema-approval}
  - id: merge_findings
    type: deterministic
    depends_on: [approve, rehearse]
    join: all_terminal
    operation: {ref: merge_review_verdicts, revision: operation-1}
    retry: {ref: twice, revision: retry-1}
    inputs:
      - name: approval
        from: {node: approve, output: approval}
      - name: review_outcome
        from: {node: code_review, receipt: terminal}
    outputs:
      - name: merged
        schema: {ref: review_verdict, revision: schema-verdict}
  - id: publish_report
    type: action
    depends_on: [merge_findings]
    operation: {ref: requirement_comment, revision: operation-2}
    inputs:
      - name: body
        from: {node: merge_findings, output: merged}
    outputs:
      - name: receipt
        schema: {ref: comment_receipt, revision: schema-receipt}
"""


def graph(document: bytes = DOCUMENT) -> WorkflowGraphV3:
    parsed = parse_workflow_document(document)
    assert isinstance(parsed, WorkflowGraphV3)
    return parsed


def with_node_line(node_id: str, line: str, document: bytes = DOCUMENT) -> bytes:
    anchor = f"  - id: {node_id}\n".encode()
    assert document.count(anchor) == 1
    return document.replace(anchor, anchor + f"    {line}\n".encode())


def with_document_line(line: str, document: bytes = DOCUMENT) -> bytes:
    anchor = b"format_version: 3\n"
    assert document.count(anchor) == 1
    return document.replace(anchor, anchor + f"{line}\n".encode())


def with_document_name(authored: str, document: bytes = DOCUMENT) -> bytes:
    anchor = NAME_LINE.encode()
    assert document.count(anchor) == 1
    return document.replace(anchor, f"name: {authored}".encode())


def without_line(line: str, document: bytes = DOCUMENT) -> bytes:
    return document.replace(f"{line}\n".encode(), b"", 1)


ONE_ROLE_TWICE = DOCUMENT.replace(b"    role: reviewer\n", b"    role: builder\n")
"""The same chain with both its agents carrying one role.

A role is the casting unit, so two nodes filled by one occupation is an
ordinary document -- and the place where two nodes can contradict each other
about what that one occupation should be.
"""

CONTRADICTED_ROLE_STATEMENTS = (
    ("difficulty", "difficulty: 3", "difficulty: 1"),
    ("kind", "kind: build", "kind: review"),
    ("family_differs_from", "family_differs_from: writer", "family_differs_from: poet"),
    ("model", "model: claude-opus-5", "model: grok-4.6"),
)
"""Every form of the role grammar, written twice over one role and disagreeing.

The contradiction is what each case is about, so the family rule names roles
nobody declares on purpose: a document that cannot say what it asks of a role is
refused for saying two things, before anything asks whether either could hold.
"""


GREEN_CONDITION = (
    "{output: rehearsal, schema: {ref: review_verdict, revision: schema-verdict}}"
)
CARRY = (
    "[{name: prior_verdict, from_output: rehearsal, "
    "seed: {node: implement, output: candidate}}]"
)


def iterate_line(
    bound: str = "maximum_rounds: 4",
    until: str = GREEN_CONDITION,
    carry: str = CARRY,
) -> str:
    """One `iterate` block as a flow mapping, so a case differs only in its data."""
    declared = [part for part in (bound, f"until: {until}", f"carry: {carry}") if part]
    return "iterate: {" + ", ".join(declared) + "}"


def loops_line(
    body: str = "[code_review, approve]",
    bound: str = "maximum_rounds: 2",
    loop_id: str = "review_cycle",
    second: str = "",
    repeat_while: str = "",
) -> str:
    """One or two loop declarations as a flow sequence, differing only in data."""
    declared = [
        part
        for part in (
            f"id: {loop_id}",
            f"body: {body}",
            bound,
            f"repeat_while: {repeat_while}" if repeat_while else "",
        )
        if part
    ]
    return "loops: [{" + ", ".join(declared) + "}" + second + "]"


APPROVAL_UNDER_THE_VERDICT_CONTRACT = b"revision: " + (
    VERDICT_ANSWER_SCHEMA.revision_hash.value.encode("ascii")
)


def answering_under_the_verdict_contract(document: bytes = DOCUMENT) -> bytes:
    """The same document, with the round's last node answering as a verdict.

    The revision is taken from the contract itself rather than written down
    here: what a document must pin to have its verdict read is one product
    decision, and a test spelling its own copy would keep passing after that
    decision moved.
    """
    anchor = b"revision: schema-approval"
    assert document.count(anchor) == 1
    return document.replace(anchor, APPROVAL_UNDER_THE_VERDICT_CONTRACT)


@pytest.mark.proves("a-loop-over-a-stretch-of-the-graph-is-declarable")
def test_a_declared_loop_reads_back_with_its_body_and_its_bound() -> None:
    parsed = graph(with_document_line(loops_line()))

    declared = parsed.loops[0]
    assert (declared.id, declared.body) == ("review_cycle", ("code_review", "approve"))
    assert declared.maximum_rounds == 2
    assert parsed.loop_of("code_review") is declared
    assert parsed.loop_of("implement") is None


@pytest.mark.proves("a-loop-over-a-stretch-of-the-graph-is-declarable")
def test_a_document_that_declares_no_loop_repeats_nothing() -> None:
    parsed = graph()

    assert parsed.loops == ()
    assert parsed.declared_rounds_of("implement") == range(1, 2)


@pytest.mark.proves("a-loop-without-a-bound-is-refused-by-name")
@pytest.mark.parametrize(
    "bound",
    (
        "",
        "maximum_rounds: 0",
        "maximum_rounds: -1",
        "maximum_rounds: 9223372036854775808",
        "maximum_rounds: many",
        "maximum_rounds: 1.5",
        "maximum_rounds: true",
    ),
    ids=(
        "absent",
        "no round at all",
        "below the declared range",
        "above the declared range",
        "a word",
        "a fraction",
        "a boolean",
    ),
)
def test_a_loop_that_declares_no_honest_round_bound_is_refused(bound: str) -> None:
    refusal = refusal_of(with_document_line(loops_line(bound=bound)))

    assert refusal.reason is WorkflowRefusalReason.UNBOUNDED_ITERATION
    assert refusal.field == "maximum_rounds"


@pytest.mark.proves("a-loop-the-graph-cannot-turn-is-refused-by-name")
@pytest.mark.parametrize(
    ("declaration", "reason", "node"),
    (
        (
            loops_line(body="[nowhere]"),
            WorkflowRefusalReason.UNKNOWN_NODE_REFERENCE,
            None,
        ),
        (
            loops_line(body="[approve, code_review]"),
            WorkflowRefusalReason.LOOP_BODY_NOT_ONE_LINE,
            "approve",
        ),
        (
            loops_line(body="[code_review, merge_findings]"),
            WorkflowRefusalReason.LOOP_BODY_NOT_ONE_LINE,
            "merge_findings",
        ),
        (
            loops_line(body="[implement, code_review]"),
            WorkflowRefusalReason.LOOP_BODY_NOT_ONE_LINE,
            "rehearse",
        ),
        (
            loops_line(
                second=", {id: second_cycle, body: [code_review], maximum_rounds: 2}"
            ),
            WorkflowRefusalReason.DUPLICATE_NAME,
            "code_review",
        ),
        (
            loops_line(
                second=", {id: review_cycle, body: [rehearse], maximum_rounds: 2}"
            ),
            WorkflowRefusalReason.DUPLICATE_NAME,
            None,
        ),
    ),
    ids=(
        "repeating a node nothing declares",
        "entered where the round would already have run",
        "a body the declared edges do not order in one stretch",
        "left at a node that is not the round's last",
        "one node claimed by two loops",
        "two loops under one id",
    ),
)
def test_a_loop_the_declared_edges_cannot_turn_is_refused(
    declaration: str, reason: WorkflowRefusalReason, node: str | None
) -> None:
    refusal = refusal_of(with_document_line(declaration))

    assert refusal.reason is reason
    assert (refusal.node, refusal.field) == (node, "loops")


@pytest.mark.proves("a-loop-may-declare-the-verdict-that-sends-it-round-again")
def test_a_declared_verdict_reads_back_with_the_node_whose_answer_says_it() -> None:
    parsed = graph(
        answering_under_the_verdict_contract(
            with_document_line(
                loops_line(repeat_while="{node: approve, verdict: revise}")
            )
        )
    )

    condition = parsed.loops[0].repeat_while
    assert condition is not None
    assert (condition.node, condition.verdict) == ("approve", Verdict.REVISE)
    assert verdict_condition_of(parsed, "approve") is condition
    assert verdict_condition_of(parsed, "code_review") is None


@pytest.mark.proves("a-loop-may-declare-the-verdict-that-sends-it-round-again")
def test_a_loop_that_declares_no_verdict_steers_nothing() -> None:
    parsed = graph(with_document_line(loops_line()))

    assert parsed.loops[0].repeat_while is None
    assert verdict_condition_of(parsed, "approve") is None


@pytest.mark.proves("a-verdict-the-loop-could-not-read-is-refused-by-name")
@pytest.mark.parametrize(
    ("document", "reason", "node", "field"),
    (
        (
            with_document_line(
                loops_line(repeat_while="{node: code_review, verdict: revise}")
            ),
            WorkflowRefusalReason.LOOP_VERDICT_NODE_NOT_THE_ROUND_END,
            "code_review",
            "loops",
        ),
        (
            with_document_line(
                loops_line(repeat_while="{node: implement, verdict: revise}")
            ),
            WorkflowRefusalReason.LOOP_VERDICT_NODE_NOT_THE_ROUND_END,
            "implement",
            "loops",
        ),
        (
            with_document_line(
                loops_line(repeat_while="{node: approve, verdict: revise}")
            ),
            WorkflowRefusalReason.LOOP_VERDICT_UNREADABLE,
            "approve",
            "loops",
        ),
        (
            answering_under_the_verdict_contract(
                with_document_line(
                    loops_line(repeat_while="{node: approve, verdict: refused}")
                )
            ),
            WorkflowRefusalReason.INVALID_VALUE,
            None,
            "verdict",
        ),
    ),
    ids=(
        "a node that does not close the round",
        "a node the loop does not even repeat",
        "an answer that pins another contract than the verdict's",
        "a word outside the closed vocabulary",
    ),
)
def test_a_verdict_condition_the_engine_could_not_honour_is_refused(
    document: bytes,
    reason: WorkflowRefusalReason,
    node: str | None,
    field: str,
) -> None:
    """Each case would leave a started run with a continuation nobody can decide."""
    refusal = refusal_of(document)

    assert refusal.reason is reason
    assert (refusal.node, refusal.field) == (node, field)


@pytest.mark.proves("a-loop-the-graph-cannot-turn-is-refused-by-name")
def test_a_backwards_control_edge_is_still_refused_as_the_cycle_it_is() -> None:
    """The loop is the only legal way back, and declaring one changes nothing here.

    A `depends_on` edge pointing at a node further down the order is what ADR
    0002 refuses unconditionally, and an unconditional refusal that gained an
    exception for the loop would stop being one.
    """
    refusal = refusal_of(
        with_node_line("implement", "depends_on: [code_review]", DOCUMENT)
    )

    assert refusal.reason is WorkflowRefusalReason.CYCLE
    assert refusal.field == "depends_on"


def test_every_node_kind_of_the_record_parses_into_its_own_closed_model() -> None:
    parsed = graph()

    assert [type(node) for node in parsed.nodes] == [
        AgentNodeV3,
        AgentNodeV3,
        SubworkflowNodeV3,
        WaitNodeV3,
        DeterministicNodeV3,
        ActionNodeV3,
    ]
    builder = parsed.node("implement")
    assert isinstance(builder, AgentNodeV3)
    assert (builder.role, builder.mode) == ("builder", "headless")
    assert builder.instruction.startswith("Implement every acceptance sentence")
    assert builder.profile is not None
    assert builder.profile.revision == "profile-1"
    assert [skill.ref for skill in builder.skills] == ["workspace_discipline"]
    assert [tool.ref for tool in builder.tools] == ["repository_write"]
    assert builder.policy is not None
    assert builder.policy.ref == "house_rules"
    assert builder.budget is not None
    assert builder.budget.ref == "build_budget"
    assert builder.retry is not None
    assert builder.retry.ref == "twice"
    assert builder.cancellation is not None
    assert builder.cancellation.ref == "drain"
    assert builder.required_context[0].source.selector == "story_acceptance"
    assert [
        operation.ref for operation in builder.available_context[0].read_operations
    ] == ["search"]
    assert builder.outputs[0].schema_reference.revision == "schema-candidate"


@pytest.mark.proves("a-bounded-iteration-is-declarable-and-reads-back-as-authored")
@pytest.mark.proves(
    "an-authored-v3-subworkflow-remains-unexecutable-without-start-writes"
)
def test_a_bounded_iteration_reads_back_with_its_bound_green_condition_and_handover() -> (
    None
):
    parsed = graph(with_node_line("rehearse", iterate_line()))

    looping = parsed.node("rehearse")
    assert isinstance(looping, SubworkflowNodeV3)
    iterate = looping.iterate
    assert iterate is not None
    assert iterate.maximum_rounds == 4
    assert iterate.until.output == "rehearsal"
    assert iterate.until.schema_reference.revision == "schema-verdict"
    handover = iterate.carry[0]
    assert (handover.name, handover.from_output) == ("prior_verdict", "rehearsal")
    assert isinstance(handover.seed, NodeOutputSource)
    assert (handover.seed.node, handover.seed.output) == ("implement", "candidate")


@pytest.mark.proves("a-bounded-iteration-is-declarable-and-reads-back-as-authored")
def test_a_subworkflow_that_declares_no_iteration_has_no_iteration() -> None:
    looping = graph().node("rehearse")

    assert isinstance(looping, SubworkflowNodeV3)
    assert looping.iterate is None


def test_the_five_input_sources_carry_their_declared_shape() -> None:
    parsed = graph()
    builder = parsed.node("implement")
    reviewer = parsed.node("code_review")
    merge = parsed.node("merge_findings")

    assert isinstance(builder.inputs[0].source, ContextEntrySource)
    assert isinstance(builder.inputs[1].source, GraphInputSource)
    assert builder.inputs[1].source.graph_input == "brief"
    assert builder.inputs[2].source is None
    assert builder.inputs[2].value == "needs-review"
    assert isinstance(reviewer.inputs[0].source, NodeOutputSource)
    assert isinstance(merge.inputs[1].source, NodeReceiptSource)
    assert merge.inputs[1].source.node == "code_review"


ORDER_READ = b"""      - name: order
        from: {graph_input: brief}
"""


@pytest.mark.proves("an-unreadable-or-unread-graph-input-is-refused-by-name")
def test_a_node_reading_a_graph_input_the_document_never_declared_is_refused() -> None:
    misread = DOCUMENT.replace(b"{graph_input: brief}", b"{graph_input: rumour}")

    with pytest.raises(InvalidWorkflowDocument) as raised:
        parse_workflow_document(misread)

    refusal = raised.value.refusal
    assert refusal is not None
    assert (refusal.reason, refusal.field, refusal.node) == (
        WorkflowRefusalReason.UNDECLARED_GRAPH_INPUT,
        "inputs",
        "implement",
    )
    assert "rumour" in refusal.detail


@pytest.mark.proves("an-unreadable-or-unread-graph-input-is-refused-by-name")
def test_a_graph_input_no_node_reads_is_refused_rather_than_demanded_for_nothing() -> (
    None
):
    assert DOCUMENT.count(ORDER_READ) == 1
    unread = DOCUMENT.replace(ORDER_READ, b"")

    with pytest.raises(InvalidWorkflowDocument) as raised:
        parse_workflow_document(unread)

    refusal = raised.value.refusal
    assert refusal is not None
    assert (refusal.reason, refusal.field, refusal.node) == (
        WorkflowRefusalReason.GRAPH_INPUT_UNREAD,
        "graph_inputs",
        None,
    )
    assert "brief" in refusal.detail


def test_an_order_is_available_to_an_entry_node_that_no_dependency_precedes() -> None:
    """A graph input is bound before the graph runs, so no data edge orders it.

    `implement` is the entry node — its dependency closure is empty — and it reads
    the order anyway. An upstream output could not be read there, which is what
    makes the fourth source a different kind of edge rather than a spelling of the
    first three.
    """
    parsed = graph()

    assert parsed.dependency_closure("implement") == frozenset()
    assert isinstance(parsed.node("implement").inputs[1].source, GraphInputSource)


def test_control_edges_alone_derive_the_entry_sink_and_dependency_sets() -> None:
    parsed = graph()

    assert parsed.entry_node_ids == ("implement",)
    assert parsed.sink_node_ids == ("publish_report",)
    assert parsed.dependency_closure("merge_findings") == frozenset(
        {"implement", "code_review", "rehearse", "approve"}
    )
    assert parsed.graph_inputs[0].schema_reference.ref == "work_brief"
    assert parsed.graph_outputs[0].source.node == "publish_report"


def test_a_wait_declares_its_prompt_and_exactly_one_answer_schema() -> None:
    waiting = graph().node("approve")

    assert isinstance(waiting, WaitNodeV3)
    assert waiting.prompt.startswith("Approve this candidate")
    assert len(waiting.outputs) == 1
    assert waiting.outputs[0].schema_reference.ref == "landing_approval"


def test_a_subworkflow_declares_the_child_revision_it_reuses() -> None:
    child = graph().node("rehearse")

    assert isinstance(child, SubworkflowNodeV3)
    assert child.workflow.ref == "review_panel"
    assert [output.name for output in child.outputs] == ["rehearsal"]


@pytest.mark.parametrize(
    ("node_id", "expected"),
    [
        ("implement", None),
        ("code_review", "all_succeeded"),
        ("merge_findings", "all_terminal"),
    ],
    ids=["no-dependency", "single-dependency-default", "fan-in"],
)
def test_the_omitted_single_dependency_join_is_all_succeeded(
    node_id: str, expected: str | None
) -> None:
    assert graph().join_of(node_id) == expected


def test_writing_the_default_join_out_over_one_dependency_means_the_same() -> None:
    spelled_out = parse_workflow_document(
        with_node_line("code_review", "join: all_succeeded")
    )

    assert isinstance(spelled_out, WorkflowGraphV3)
    assert spelled_out.join_of("code_review") == graph().join_of("code_review")


def test_all_terminal_stays_authorable_over_a_single_dependency() -> None:
    parsed = parse_workflow_document(with_node_line("approve", "join: all_terminal"))

    assert isinstance(parsed, WorkflowGraphV3)
    assert parsed.join_of("approve") == "all_terminal"


DESCRIBED_DOCUMENT = with_document_line("description: Two reviews, one merged verdict.")
# Every boundary Python's own str.splitlines breaks a line at, which is the set
# YAML can decode into a scalar: LF, CR, VT, FF, the three separators, NEL, LS, PS.
UNICODE_LINE_BOUNDARIES = "\n\r\v\f\x1c\x1d\x1e\x85\u2028\u2029"


def test_a_document_carries_the_name_an_operator_picks_it_by() -> None:
    described = graph(DESCRIBED_DOCUMENT)

    assert (described.name, described.description) == (
        DOCUMENT_NAME,
        "Two reviews, one merged verdict.",
    )
    assert graph().description is None


@pytest.mark.parametrize(
    "boundary", UNICODE_LINE_BOUNDARIES, ids=lambda char: f"U+{ord(char):04X}"
)
def test_a_name_broken_across_any_unicode_line_boundary_is_refused(
    boundary: str,
) -> None:
    two_lines = with_document_name(f'"picker line\\u{ord(boundary):04x}hidden line"')

    with pytest.raises(InvalidWorkflowDocument) as raised:
        parse_workflow_document(two_lines)

    refusal = raised.value.refusal
    assert refusal is not None
    assert (refusal.reason, refusal.node, refusal.field) == (
        WorkflowRefusalReason.INVALID_VALUE,
        None,
        "name",
    )


def test_renaming_a_document_authors_a_new_revision() -> None:
    original = WorkflowRevision(DOCUMENT)
    renamed = WorkflowRevision(with_document_name("Renamed"))

    assert renamed.revision_hash != original.revision_hash
    assert graph(renamed.document).nodes == graph(original.document).nodes


def test_no_node_can_read_the_document_name_as_a_value() -> None:
    reading_the_name = DOCUMENT.replace(
        b"        from: {context: story}\n", b"        from: {document: name}\n"
    )

    with pytest.raises(InvalidWorkflowDocument) as raised:
        parse_workflow_document(reading_the_name)

    refusal = raised.value.refusal
    assert refusal is not None
    assert (refusal.reason, refusal.node, refusal.field) == (
        WorkflowRefusalReason.INVALID_VALUE,
        "implement",
        "from",
    )


def test_a_parsed_v3_document_is_deeply_immutable() -> None:
    parsed = graph()

    with pytest.raises(AttributeError):
        cast(MutableSequence[object], parsed.nodes).clear()
    with pytest.raises(ValueError, match="frozen"):
        parsed.node("implement").id = "renamed"


type RefusalCase = tuple[bytes, WorkflowRefusalReason, str | None, str]

CONTRADICTED_ROLE_REFUSALS: dict[str, RefusalCase] = {
    f"one-role-asking-for-two-{field}-statements": (
        with_node_line(
            "code_review", second, with_node_line("implement", first, ONE_ROLE_TWICE)
        ),
        WorkflowRefusalReason.INVALID_VALUE,
        "code_review",
        field,
    )
    for field, first, second in CONTRADICTED_ROLE_STATEMENTS
}

REFUSALS: dict[str, RefusalCase] = {
    "unknown-document-field": (
        DOCUMENT + b"owner: felix\n",
        WorkflowRefusalReason.UNKNOWN_FIELD,
        None,
        "owner",
    ),
    "document-that-names-itself-nowhere": (
        without_line(NAME_LINE),
        WorkflowRefusalReason.MISSING_FIELD,
        None,
        "name",
    ),
    "non-string-document-name": (
        with_document_name("7"),
        WorkflowRefusalReason.INVALID_VALUE,
        None,
        "name",
    ),
    "blank-document-name": (
        with_document_name("' '"),
        WorkflowRefusalReason.INVALID_VALUE,
        None,
        "name",
    ),
    "oversized-document-name": (
        with_document_name("x" * (MAXIMUM_DOCUMENT_NAME_BYTES + 1)),
        WorkflowRefusalReason.INVALID_VALUE,
        None,
        "name",
    ),
    "non-string-document-description": (
        with_document_line("description: 7"),
        WorkflowRefusalReason.INVALID_VALUE,
        None,
        "description",
    ),
    "oversized-document-description": (
        with_document_line(
            "description: " + "x" * (MAXIMUM_DOCUMENT_DESCRIPTION_BYTES + 1)
        ),
        WorkflowRefusalReason.INVALID_VALUE,
        None,
        "description",
    ),
    "description-refused-by-a-node": (
        with_node_line("implement", "description: a node carries none"),
        WorkflowRefusalReason.REFUSED_FIELD,
        "implement",
        "description",
    ),
    "unknown-node-field": (
        with_node_line("implement", "surprise: yes"),
        WorkflowRefusalReason.UNKNOWN_FIELD,
        "implement",
        "surprise",
    ),
    "budget-refused-by-deterministic": (
        with_node_line("merge_findings", "budget: {ref: b, revision: r}"),
        WorkflowRefusalReason.REFUSED_FIELD,
        "merge_findings",
        "budget",
    ),
    "available-context-refused-by-wait": (
        with_node_line("approve", "available_context: []"),
        WorkflowRefusalReason.REFUSED_FIELD,
        "approve",
        "available_context",
    ),
    "retry-refused-by-action": (
        with_node_line("publish_report", "retry: {ref: twice, revision: retry-1}"),
        WorkflowRefusalReason.REFUSED_FIELD,
        "publish_report",
        "retry",
    ),
    "instruction-refused-by-deterministic": (
        with_node_line("merge_findings", "instruction: do the work"),
        WorkflowRefusalReason.REFUSED_FIELD,
        "merge_findings",
        "instruction",
    ),
    "required-context-refused-by-subworkflow": (
        with_node_line("rehearse", "required_context: []"),
        WorkflowRefusalReason.REFUSED_FIELD,
        "rehearse",
        "required_context",
    ),
    "prompt-refused-by-agent": (
        with_node_line("implement", "prompt: decide"),
        WorkflowRefusalReason.REFUSED_FIELD,
        "implement",
        "prompt",
    ),
    "missing-mode": (
        without_line("    mode: headless"),
        WorkflowRefusalReason.MISSING_FIELD,
        "implement",
        "mode",
    ),
    "missing-operation": (
        without_line(
            "    operation: {ref: requirement_comment, revision: operation-2}"
        ),
        WorkflowRefusalReason.MISSING_FIELD,
        "publish_report",
        "operation",
    ),
    "unpinned-reference": (
        DOCUMENT.replace(
            b"profile: {ref: builder_method, revision: profile-1}",
            b"profile: {ref: builder_method}",
        ),
        WorkflowRefusalReason.MISSING_FIELD,
        "implement",
        "revision",
    ),
    "empty-instruction": (
        DOCUMENT.replace(
            b"instruction: Name every defect with the sentence it violates.",
            b"instruction: ' '",
        ),
        WorkflowRefusalReason.INVALID_VALUE,
        "code_review",
        "instruction",
    ),
    "oversized-instruction": (
        DOCUMENT.replace(
            b"instruction: Name every defect with the sentence it violates.",
            b"instruction: " + b"x" * (MAXIMUM_INSTRUCTION_BYTES + 1),
        ),
        WorkflowRefusalReason.INVALID_VALUE,
        "code_review",
        "instruction",
    ),
    "wait-without-exactly-one-output": (
        DOCUMENT.replace(
            b"      - name: approval\n"
            b"        schema: {ref: landing_approval, revision: schema-approval}\n",
            b"      - name: approval\n"
            b"        schema: {ref: landing_approval, revision: schema-approval}\n"
            b"      - name: second\n"
            b"        schema: {ref: landing_approval, revision: schema-approval}\n",
        ),
        WorkflowRefusalReason.INVALID_VALUE,
        "approve",
        "outputs",
    ),
    "grant-without-read-operation": (
        DOCUMENT.replace(
            b"        read_operations:\n          - {ref: search, revision: read-1}\n",
            b"        read_operations: []\n",
        ),
        WorkflowRefusalReason.INVALID_VALUE,
        "implement",
        "read_operations",
    ),
    "unknown-node-kind": (
        DOCUMENT.replace(b"    type: wait\n", b"    type: mystery\n"),
        WorkflowRefusalReason.INVALID_VALUE,
        "approve",
        "type",
    ),
    "missing-node-kind": (
        without_line("    type: wait"),
        WorkflowRefusalReason.MISSING_FIELD,
        "approve",
        "type",
    ),
    "unknown-input-source-form": (
        DOCUMENT.replace(
            b"        from: {context: story}\n", b"        from: {elsewhere: story}\n"
        ),
        WorkflowRefusalReason.INVALID_VALUE,
        "implement",
        "from",
    ),
    "no-nodes": (
        f"format_version: 3\n{NAME_LINE}\nnodes: []\n".encode(),
        WorkflowRefusalReason.INVALID_VALUE,
        None,
        "nodes",
    ),
    "input-without-a-source": (
        DOCUMENT.replace(b"        from: {context: story}\n", b""),
        WorkflowRefusalReason.INVALID_VALUE,
        "implement",
        "inputs",
    ),
    "input-with-two-sources": (
        DOCUMENT.replace(
            b"      - name: label\n        value: needs-review\n",
            b"      - name: label\n"
            b"        value: needs-review\n"
            b"        from: {context: story}\n",
        ),
        WorkflowRefusalReason.INVALID_VALUE,
        "implement",
        "inputs",
    ),
    "input-source-in-no-declared-form": (
        DOCUMENT.replace(
            b"        from: {node: implement, output: candidate}\n",
            b"        from: {node: implement}\n",
            1,
        ),
        WorkflowRefusalReason.INVALID_VALUE,
        "code_review",
        "from",
    ),
    "duplicate-node-id": (
        DOCUMENT.replace(b"  - id: rehearse\n", b"  - id: code_review\n"),
        WorkflowRefusalReason.DUPLICATE_NODE_ID,
        "code_review",
        "id",
    ),
    "duplicate-input-name": (
        DOCUMENT.replace(
            b"      - name: review_outcome\n", b"      - name: approval\n"
        ),
        WorkflowRefusalReason.DUPLICATE_NAME,
        "merge_findings",
        "inputs",
    ),
    "duplicate-output-name": (
        DOCUMENT.replace(
            b"      - name: candidate\n"
            b"        schema: {ref: workspace_candidate, revision: schema-candidate}\n",
            b"      - name: candidate\n"
            b"        schema: {ref: workspace_candidate, revision: schema-candidate}\n"
            b"      - name: candidate\n"
            b"        schema: {ref: workspace_candidate, revision: schema-candidate}\n",
        ),
        WorkflowRefusalReason.DUPLICATE_NAME,
        "implement",
        "outputs",
    ),
    "duplicate-dependency": (
        DOCUMENT.replace(
            b"    depends_on: [approve, rehearse]\n",
            b"    depends_on: [approve, approve]\n",
        ),
        WorkflowRefusalReason.DUPLICATE_NAME,
        "merge_findings",
        "depends_on",
    ),
    "duplicate-graph-output-name": (
        DOCUMENT.replace(
            b"  - name: landed\n    from: {node: publish_report, output: receipt}\n",
            b"  - name: landed\n    from: {node: publish_report, output: receipt}\n"
            b"  - name: landed\n    from: {node: publish_report, output: receipt}\n",
        ),
        WorkflowRefusalReason.DUPLICATE_NAME,
        None,
        "graph_outputs",
    ),
    "unknown-dependency": (
        DOCUMENT.replace(
            b"    depends_on: [code_review]\n", b"    depends_on: [gone]\n"
        ),
        WorkflowRefusalReason.UNKNOWN_NODE_REFERENCE,
        "approve",
        "depends_on",
    ),
    "self-dependency": (
        DOCUMENT.replace(
            b"    depends_on: [code_review]\n", b"    depends_on: [approve]\n"
        ),
        WorkflowRefusalReason.CYCLE,
        "approve",
        "depends_on",
    ),
    "cycle": (
        with_node_line("implement", "depends_on: [publish_report]"),
        WorkflowRefusalReason.CYCLE,
        "implement",
        "depends_on",
    ),
    "join-without-dependency": (
        with_node_line("implement", "join: all_succeeded"),
        WorkflowRefusalReason.JOIN_WITHOUT_DEPENDENCY,
        "implement",
        "join",
    ),
    "join-missing-on-fan-in": (
        without_line("    join: all_terminal"),
        WorkflowRefusalReason.JOIN_REQUIRED,
        "merge_findings",
        "join",
    ),
    "data-edge-outside-the-dependency-closure": (
        DOCUMENT.replace(
            b"        from: {node: implement, output: candidate}\n",
            b"        from: {node: approve, output: approval}\n",
            1,
        ),
        WorkflowRefusalReason.DATA_EDGE_OUTSIDE_CLOSURE,
        "code_review",
        "inputs",
    ),
    "data-edge-to-an-undeclared-output": (
        DOCUMENT.replace(
            b"        from: {node: implement, output: candidate}\n",
            b"        from: {node: implement, output: absent}\n",
            1,
        ),
        WorkflowRefusalReason.UNDECLARED_OUTPUT,
        "code_review",
        "inputs",
    ),
    "input-reading-an-undeclared-context": (
        DOCUMENT.replace(
            b"        from: {context: story}\n", b"        from: {context: absent}\n"
        ),
        WorkflowRefusalReason.UNDECLARED_CONTEXT,
        "implement",
        "inputs",
    ),
    "graph-output-from-a-non-sink": (
        DOCUMENT.replace(
            b"    from: {node: publish_report, output: receipt}\n",
            b"    from: {node: implement, output: candidate}\n",
        ),
        WorkflowRefusalReason.GRAPH_OUTPUT_NOT_SINK,
        None,
        "graph_outputs",
    ),
    "graph-output-from-an-unknown-node": (
        DOCUMENT.replace(
            b"    from: {node: publish_report, output: receipt}\n",
            b"    from: {node: gone, output: receipt}\n",
        ),
        WorkflowRefusalReason.UNKNOWN_NODE_REFERENCE,
        None,
        "graph_outputs",
    ),
    "graph-output-of-an-undeclared-output": (
        DOCUMENT.replace(
            b"    from: {node: publish_report, output: receipt}\n",
            b"    from: {node: publish_report, output: absent}\n",
        ),
        WorkflowRefusalReason.UNDECLARED_OUTPUT,
        None,
        "graph_outputs",
    ),
    "interactive-output-without-operator-confirmation": (
        DOCUMENT.replace(b"    mode: headless\n", b"    mode: interactive\n", 1),
        WorkflowRefusalReason.UNCONFIRMED_INTERACTIVE_OUTPUT,
        "implement",
        "outputs",
    ),
    "operator-confirmation-without-interactive-mode": (
        DOCUMENT.replace(
            b"      - name: receipt\n",
            b"      - name: receipt\n        confirmed_by: operator\n",
        ),
        WorkflowRefusalReason.CONFIRMATION_WITHOUT_INTERACTIVE_MODE,
        "publish_report",
        "outputs",
    ),
    "difficulty-outside-the-three": (
        with_node_line("implement", "difficulty: 4"),
        WorkflowRefusalReason.INVALID_VALUE,
        "implement",
        "difficulty",
    ),
    "difficulty-written-as-a-name": (
        with_node_line("implement", "difficulty: hard"),
        WorkflowRefusalReason.INVALID_VALUE,
        "implement",
        "difficulty",
    ),
    "difficulty-written-as-a-flag": (
        with_node_line("implement", "difficulty: true"),
        WorkflowRefusalReason.INVALID_VALUE,
        "implement",
        "difficulty",
    ),
    "difficulty-written-as-a-decimal": (
        with_node_line("implement", "difficulty: 2.0"),
        WorkflowRefusalReason.INVALID_VALUE,
        "implement",
        "difficulty",
    ),
    "difficulty-refused-by-a-wait": (
        with_node_line("approve", "difficulty: 3"),
        WorkflowRefusalReason.REFUSED_FIELD,
        "approve",
        "difficulty",
    ),
    "kind-outside-the-two": (
        with_node_line("implement", "kind: audit"),
        WorkflowRefusalReason.INVALID_VALUE,
        "implement",
        "kind",
    ),
    "kind-written-as-a-number": (
        with_node_line("implement", "kind: 2"),
        WorkflowRefusalReason.INVALID_VALUE,
        "implement",
        "kind",
    ),
    "kind-refused-by-a-wait": (
        with_node_line("approve", "kind: review"),
        WorkflowRefusalReason.REFUSED_FIELD,
        "approve",
        "kind",
    ),
    "model-pinned-as-an-alias": (
        with_node_line("implement", "model: newest opus"),
        WorkflowRefusalReason.INVALID_VALUE,
        "implement",
        "model",
    ),
    "family-rule-naming-a-role-nothing-declares": (
        with_node_line("implement", "family_differs_from: ghost"),
        WorkflowRefusalReason.UNDECLARED_ROLE,
        "implement",
        "family_differs_from",
    ),
    "family-rule-naming-its-own-role": (
        with_node_line("implement", "family_differs_from: builder"),
        WorkflowRefusalReason.INVALID_VALUE,
        "implement",
        "family_differs_from",
    ),
} | CONTRADICTED_ROLE_REFUSALS


def refusal_of(document: bytes) -> WorkflowRefusal:
    with pytest.raises(InvalidWorkflowDocument) as raised:
        parse_workflow_document(document)
    refusal = raised.value.refusal
    assert refusal is not None
    return refusal


@pytest.mark.proves("an-iteration-without-a-bound-is-refused-by-name")
@pytest.mark.parametrize(
    "bound",
    (
        "",
        "maximum_rounds: 0",
        "maximum_rounds: -1",
        "maximum_rounds: 9223372036854775808",
        "maximum_rounds: four",
        "maximum_rounds: 4.0",
        "maximum_rounds: true",
    ),
    ids=(
        "absent",
        "no round at all",
        "negative",
        "above the declared range",
        "a word",
        "a fraction",
        "a boolean",
    ),
)
def test_an_iteration_that_declares_no_honest_round_bound_is_refused(
    bound: str,
) -> None:
    refusal = refusal_of(with_node_line("rehearse", iterate_line(bound=bound)))

    assert refusal.reason is WorkflowRefusalReason.UNBOUNDED_ITERATION
    assert (refusal.node, refusal.field) == ("rehearse", "maximum_rounds")


@pytest.mark.proves("a-green-condition-the-document-contradicts-is-refused-by-name")
def test_a_green_condition_under_another_schema_revision_than_the_node_declares_is_refused() -> (
    None
):
    contradicted = with_node_line(
        "rehearse",
        iterate_line(
            until=(
                "{output: rehearsal, "
                "schema: {ref: review_verdict, revision: schema-other}}"
            )
        ),
    )

    refusal = refusal_of(contradicted)

    assert refusal.reason is WorkflowRefusalReason.ITERATION_GREEN_CONDITION_UNPROVABLE
    assert (refusal.node, refusal.field) == ("rehearse", "iterate")


@pytest.mark.proves("a-round-handover-unbound-in-the-document-is-refused-by-name")
@pytest.mark.parametrize(
    ("carry", "reason", "field"),
    (
        (
            "[{name: prior_verdict, from_output: rehearsal}]",
            WorkflowRefusalReason.ITERATION_CARRY_UNBOUND,
            "seed",
        ),
        (
            (
                "[{name: prior_verdict, from_output: rehearsal, "
                "seed: {node: implement, output: candidate}}, "
                "{name: prior_verdict, from_output: rehearsal, "
                "seed: {node: implement, output: candidate}}]"
            ),
            WorkflowRefusalReason.ITERATION_CARRY_UNBOUND,
            "iterate",
        ),
        (
            (
                "[{name: prior_verdict, from_output: rehearsal, "
                "seed: {node: code_review, output: findings}}]"
            ),
            WorkflowRefusalReason.DATA_EDGE_OUTSIDE_CLOSURE,
            "iterate",
        ),
        (
            (
                "[{name: prior_verdict, from_output: rehearsal, "
                "seed: {node: implement, output: absent}}]"
            ),
            WorkflowRefusalReason.UNDECLARED_OUTPUT,
            "iterate",
        ),
        (
            (
                "[{name: prior_verdict, from_output: rehearsal, "
                "seed: {graph_input: rumour}}]"
            ),
            WorkflowRefusalReason.UNDECLARED_GRAPH_INPUT,
            "iterate",
        ),
    ),
    ids=(
        "no seed for the first round",
        "the same handover twice",
        "seeded outside the closure",
        "seeded from an undeclared output",
        "seeded from an undeclared graph input",
    ),
)
def test_a_round_handover_the_document_cannot_bind_is_refused(
    carry: str, reason: WorkflowRefusalReason, field: str
) -> None:
    refusal = refusal_of(with_node_line("rehearse", iterate_line(carry=carry)))

    assert refusal.reason is reason
    assert (refusal.node, refusal.field) == ("rehearse", field)


@pytest.mark.proves("iteration-is-refused-on-a-node-kind-that-cannot-carry-it")
@pytest.mark.parametrize(
    "node_id",
    ("implement", "approve", "merge_findings", "publish_report"),
    ids=("agent", "wait", "deterministic", "action"),
)
def test_iteration_is_refused_on_every_node_kind_that_cannot_carry_it(
    node_id: str,
) -> None:
    refusal = refusal_of(with_node_line(node_id, iterate_line()))

    assert refusal.reason is WorkflowRefusalReason.REFUSED_FIELD
    assert (refusal.node, refusal.field) == (node_id, "iterate")


REFUSAL_LINE = "        refusal: {reason: required}"


@pytest.mark.proves("a-node-may-declare-a-named-agent-refusal")
def test_a_declared_output_refusal_reads_back_as_authored() -> None:
    document = DOCUMENT.replace(
        b"        schema: {ref: workspace_candidate, revision: schema-candidate}\n",
        b"        schema: {ref: workspace_candidate, revision: schema-candidate}\n"
        + REFUSAL_LINE.encode()
        + b"\n",
        1,
    )
    parsed = graph(document)
    implement = parsed.node("implement")
    assert isinstance(implement, AgentNodeV3)
    assert implement.outputs[0].refusal is not None
    assert implement.outputs[0].refusal.reason == "required"
    review = parsed.node("code_review")
    assert isinstance(review, AgentNodeV3)
    assert review.outputs[0].refusal is None


@pytest.mark.proves("a-node-may-declare-a-named-agent-refusal")
def test_an_output_without_a_refusal_declaration_has_none() -> None:
    implement = graph().node("implement")
    assert isinstance(implement, AgentNodeV3)
    assert implement.outputs[0].refusal is None


CONSTRAINT = "binding_constraint: {distinct_from: implement}"


@pytest.mark.proves("a-node-may-declare-it-must-not-share-another-nodes-binding")
def test_a_declared_distinct_from_reads_back_as_authored() -> None:
    parsed = graph(with_node_line("code_review", CONSTRAINT))

    constrained = parsed.node("code_review")
    assert isinstance(constrained, AgentNodeV3)
    assert constrained.binding_constraint is not None
    assert constrained.binding_constraint.distinct_from == "implement"
    unbound = parsed.node("implement")
    assert isinstance(unbound, AgentNodeV3)
    assert unbound.binding_constraint is None


@pytest.mark.proves("a-node-may-declare-it-must-not-share-another-nodes-binding")
def test_an_undeclared_binding_constraint_is_absent() -> None:
    review = graph().node("code_review")
    assert isinstance(review, AgentNodeV3)
    assert review.binding_constraint is None


@pytest.mark.proves("a-distinct-from-the-document-cannot-honour-is-refused-by-name")
@pytest.mark.parametrize(
    ("line", "reason", "detail"),
    (
        (
            "binding_constraint: {distinct_from: ghost}",
            WorkflowRefusalReason.UNKNOWN_NODE_REFERENCE,
            "ghost",
        ),
        (
            "binding_constraint: {distinct_from: code_review}",
            WorkflowRefusalReason.INVALID_VALUE,
            "this node",
        ),
        (
            "binding_constraint: {distinct_from: approve}",
            WorkflowRefusalReason.INVALID_VALUE,
            "no agent binding",
        ),
    ),
    ids=("unknown", "self", "wait node"),
)
def test_a_distinct_from_the_document_cannot_honour_is_refused(
    line: str, reason: WorkflowRefusalReason, detail: str
) -> None:
    refusal = refusal_of(with_node_line("code_review", line))

    assert refusal.reason is reason
    assert (refusal.node, refusal.field) == ("code_review", "binding_constraint")
    assert detail in refusal.detail


@pytest.mark.proves("a-distinct-from-the-document-cannot-honour-is-refused-by-name")
def test_distinct_from_a_node_that_shares_this_role_is_refused() -> None:
    document = DOCUMENT.replace(b"    role: reviewer\n", b"    role: builder\n", 1)
    refusal = refusal_of(with_node_line("code_review", CONSTRAINT, document))

    assert refusal.reason is WorkflowRefusalReason.INVALID_VALUE
    assert (refusal.node, refusal.field) == ("code_review", "binding_constraint")
    assert "shares role" in refusal.detail


@pytest.mark.proves("a-distinct-from-the-document-cannot-honour-is-refused-by-name")
def test_binding_constraint_is_refused_on_a_node_without_a_binding() -> None:
    refusal = refusal_of(with_node_line("approve", CONSTRAINT))

    assert refusal.reason is WorkflowRefusalReason.REFUSED_FIELD
    assert (refusal.node, refusal.field) == ("approve", "binding_constraint")


@pytest.mark.proves("a-distinct-from-the-document-cannot-honour-is-refused-by-name")
def test_the_document_gate_is_what_refuses_an_unknown_distinct_from(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "atelier2.contracts.workflows_v3._refuse_broken_binding_constraints",
        lambda _graph: None,
    )
    parsed = graph(
        with_node_line("code_review", "binding_constraint: {distinct_from: ghost}")
    )
    constrained = parsed.node("code_review")
    assert isinstance(constrained, AgentNodeV3)
    assert constrained.binding_constraint is not None
    assert constrained.binding_constraint.distinct_from == "ghost"


@pytest.mark.parametrize(
    ("document", "reason", "node", "field"), REFUSALS.values(), ids=REFUSALS
)
def test_every_refused_v3_form_names_its_node_and_field(
    document: bytes,
    reason: WorkflowRefusalReason,
    node: str | None,
    field: str,
) -> None:
    with pytest.raises(InvalidWorkflowDocument) as raised:
        parse_workflow_document(document)

    refusal = raised.value.refusal
    assert refusal is not None
    assert (refusal.reason, refusal.node, refusal.field) == (reason, node, field)


SILENT_BUILDER = DeclaredRole(
    "builder", DEFAULT_ROLE_DIFFICULTY, DEFAULT_ROLE_KIND, None, None
)
SILENT_REVIEWER = DeclaredRole(
    "reviewer", DEFAULT_ROLE_DIFFICULTY, DEFAULT_ROLE_KIND, None, None
)

ROLE_DECLARATIONS: dict[str, tuple[bytes, tuple[DeclaredRole, ...]]] = {
    "a document that asks nothing beyond the role names": (
        DOCUMENT,
        (SILENT_BUILDER, SILENT_REVIEWER),
    ),
    "a role naming every form the grammar has": (
        with_node_line(
            "code_review",
            "kind: review",
            with_node_line(
                "implement",
                "model: claude-opus-5",
                with_node_line(
                    "implement",
                    "family_differs_from: reviewer",
                    with_node_line("implement", "difficulty: 3"),
                ),
            ),
        ),
        (
            DeclaredRole("builder", 3, "build", "reviewer", "claude-opus-5"),
            DeclaredRole("reviewer", DEFAULT_ROLE_DIFFICULTY, "review", None, None),
        ),
    ),
    "one role carried by two nodes, one of them silent": (
        with_node_line("implement", "difficulty: 1", ONE_ROLE_TWICE),
        (DeclaredRole("builder", 1, DEFAULT_ROLE_KIND, None, None),),
    ),
    "a role whose pin is written as nothing": (
        with_node_line("implement", "model: null"),
        (SILENT_BUILDER, SILENT_REVIEWER),
    ),
}


@pytest.mark.parametrize(
    ("document", "declared"), ROLE_DECLARATIONS.values(), ids=ROLE_DECLARATIONS
)
def test_a_document_says_once_per_role_what_it_asks_of_whoever_fills_it(
    document: bytes, declared: tuple[DeclaredRole, ...]
) -> None:
    """The seam the casting reads: one declaration per role, never per node."""
    assert declared_roles_of(graph(document)) == declared


@pytest.mark.parametrize("retired", RETIRED_KEY_REPLACEMENTS)
def test_every_retired_v1_or_v2_key_is_refused_naming_its_replacement(
    retired: str,
) -> None:
    document = with_node_line("implement", f"{retired}: carried-over")

    with pytest.raises(InvalidWorkflowDocument) as raised:
        parse_workflow_document(document)

    refusal = raised.value.refusal
    assert refusal is not None
    assert refusal.reason is WorkflowRefusalReason.RETIRED_KEY
    assert (refusal.node, refusal.field) == ("implement", retired)
    assert RETIRED_KEY_REPLACEMENTS[retired] in refusal.detail


def test_a_retired_key_is_named_at_the_document_level_too() -> None:
    with pytest.raises(InvalidWorkflowDocument) as raised:
        parse_workflow_document(DOCUMENT + b"start: implement\n")

    refusal = raised.value.refusal
    assert refusal is not None
    assert (refusal.reason, refusal.node, refusal.field) == (
        WorkflowRefusalReason.RETIRED_KEY,
        None,
        "start",
    )


UNSAFE_V3_DOCUMENTS: dict[str, tuple[bytes, WorkflowRefusalReason, str]] = {
    "anchor": (
        b"format_version: 3\nnodes: &nodes []\ncopy: *nodes\n",
        WorkflowRefusalReason.FORBIDDEN_YAML_FEATURE,
        "anchor",
    ),
    "alias": (
        b"format_version: 3\nnodes: *nodes\n",
        WorkflowRefusalReason.FORBIDDEN_YAML_FEATURE,
        "alias",
    ),
    "merge": (
        b"format_version: 3\nnodes: [{<<: {id: a}, type: agent}]\n",
        WorkflowRefusalReason.FORBIDDEN_YAML_FEATURE,
        "<<",
    ),
    "explicit-tag": (
        b"format_version: !!int 3\nnodes: []\n",
        WorkflowRefusalReason.FORBIDDEN_YAML_FEATURE,
        "tag",
    ),
    "unhashable-key": (
        b"format_version: 3\nnodes:\n  ? [a, b]\n  : c\n",
        WorkflowRefusalReason.FORBIDDEN_YAML_FEATURE,
        "key",
    ),
    "bom": (
        b"\xef\xbb\xbfformat_version: 3\nnodes: []\n",
        WorkflowRefusalReason.MALFORMED_DOCUMENT,
        "encoding",
    ),
    "invalid-utf8": (
        b"format_version: 3\nnodes: \xff\n",
        WorkflowRefusalReason.MALFORMED_DOCUMENT,
        "encoding",
    ),
    "empty": (b"", WorkflowRefusalReason.MALFORMED_DOCUMENT, "document"),
    "blank": (b"\n", WorkflowRefusalReason.MALFORMED_DOCUMENT, "document"),
    "unparsable": (
        b"format_version: 3\nnodes: [\n",
        WorkflowRefusalReason.MALFORMED_DOCUMENT,
        "syntax",
    ),
    "duplicate-key": (
        DOCUMENT + b"format_version: 3\n",
        WorkflowRefusalReason.DUPLICATE_KEY,
        "format_version",
    ),
    "multiple-documents": (
        DOCUMENT + b"---\n{}\n",
        WorkflowRefusalReason.MULTIPLE_DOCUMENTS,
        "---",
    ),
    "deeper-than-the-nesting-bound": (
        b"format_version: 3\nnodes: "
        + b"[" * (MAXIMUM_DOCUMENT_DEPTH + 1)
        + b"]" * (MAXIMUM_DOCUMENT_DEPTH + 1)
        + b"\n",
        WorkflowRefusalReason.DOCUMENT_TOO_DEEP,
        "nesting",
    ),
    "unsupported-format-version": (
        b"format_version: 9\nnodes: []\n",
        WorkflowRefusalReason.INVALID_VALUE,
        "format_version",
    ),
    "non-integer-format-version": (
        b"format_version: three\nnodes: []\n",
        WorkflowRefusalReason.INVALID_VALUE,
        "format_version",
    ),
}


@pytest.mark.parametrize(
    ("document", "reason", "field"),
    UNSAFE_V3_DOCUMENTS.values(),
    ids=UNSAFE_V3_DOCUMENTS,
)
def test_unsafe_yaml_is_refused_by_name_before_any_v3_vocabulary_is_read(
    document: bytes, reason: WorkflowRefusalReason, field: str
) -> None:
    with pytest.raises(InvalidWorkflowDocument) as raised:
        parse_workflow_document(document)

    refusal = raised.value.refusal
    assert refusal is not None
    assert (refusal.reason, refusal.node, refusal.field) == (reason, None, field)


def test_a_document_nested_past_the_bound_is_refused_instead_of_exhausting_the_stack() -> (
    None
):
    document = b"format_version: 3\nnodes: " + b"[" * 20_000 + b"]" * 20_000 + b"\n"

    with pytest.raises(InvalidWorkflowDocument) as raised:
        parse_workflow_document(document)

    refusal = raised.value.refusal
    assert refusal is not None
    assert refusal.reason is WorkflowRefusalReason.DOCUMENT_TOO_DEEP


DECLARED_OUTPUT = b"""    outputs:
      - name: result
        schema: {ref: any-json, revision: "%s"}
""" % (b"e" * 64)
"""The one output `single-json-output/v1` requires, in the form an author writes.

These documents are parsed rather than run, so the revision it pins is a
placeholder of the right shape: what is under test is the form the executable
admission requires, not the schema a run would resolve.
"""

ONE_AGENT_DOCUMENT = (
    b"""format_version: 3
name: One agent
nodes:
  - id: implement
    type: agent
    role: builder
    mode: headless
    instruction: Do the one thing this chain is for.
"""
    + DECLARED_OUTPUT
)


@pytest.mark.proves("every-v3-shape-no-runtime-binds-is-refused-by-name")
def test_a_v3_document_whose_kinds_no_runtime_interprets_is_still_refused() -> None:
    """The pin stays for every kind the runtime does not interpret.

    This is the half of the old sentence that is still true, kept as its own
    case rather than reinterpreted: the document below carries deterministic,
    subworkflow and action nodes, and nothing executes any of them. The Wait
    kind left this list when the pause and its answer became executable, and
    it is admitted below rather than quietly dropped from here.
    """
    with pytest.raises(InvalidWorkflowDocument, match=FORMAT_V3_NOT_EXECUTABLE):
        parse_executable_workflow_document(DOCUMENT)


TWO_AGENT_CHAIN = (
    b"""format_version: 3
name: Two agents
nodes:
  - id: implement
    type: agent
    role: builder
    mode: headless
    instruction: Do the first thing.
"""
    + DECLARED_OUTPUT
    + b"""  - id: review
    type: agent
    role: reviewer
    mode: headless
    instruction: Judge the first thing.
    depends_on: [implement]
"""
    + DECLARED_OUTPUT
)


AGENT_WAIT_AGENT_CHAIN = (
    b"""format_version: 3
name: A person answers between two agents
nodes:
  - id: implement
    type: agent
    role: builder
    mode: headless
    instruction: Do the first thing.
"""
    + DECLARED_OUTPUT
    + b"""  - id: approve
    type: wait
    prompt: Is this good enough to review?
    depends_on: [implement]
"""
    + DECLARED_OUTPUT
    + b"""  - id: review
    type: agent
    role: reviewer
    mode: headless
    instruction: Judge the first thing.
    depends_on: [approve]
"""
    + DECLARED_OUTPUT
)

AGENT_THEN_WAIT_CHAIN = (
    b"""format_version: 3
name: A person answers last
nodes:
  - id: implement
    type: agent
    role: builder
    mode: headless
    instruction: Do the first thing.
"""
    + DECLARED_OUTPUT
    + b"""  - id: approve
    type: wait
    prompt: Is this good enough to land?
    depends_on: [implement]
"""
    + DECLARED_OUTPUT
)


@pytest.mark.proves("a-v3-agent-document-starts-and-binds-its-node")
def test_the_one_admitted_v3_shape_is_executable() -> None:
    """One simple Agent node, its own entry and its own sink."""
    parsed = parse_executable_workflow_document(ONE_AGENT_DOCUMENT)

    assert isinstance(parsed, WorkflowGraphV3)
    assert parsed.entry_node_ids == ("implement",)
    assert len(parsed.sink_node_ids) == 1


def test_a_line_of_agent_nodes_is_executable() -> None:
    """`depends_on` is bound where it names one edge: after a node, its one heir."""
    parsed = parse_executable_workflow_document(TWO_AGENT_CHAIN)

    assert isinstance(parsed, WorkflowGraphV3)
    assert parsed.entry_node_ids == ("implement",)
    assert parsed.sink_node_ids == ("review",)


@pytest.mark.parametrize(
    ("document", "sink"),
    [(AGENT_WAIT_AGENT_CHAIN, "review"), (AGENT_THEN_WAIT_CHAIN, "approve")],
    ids=["a wait between two agents", "a wait as the line's sink"],
)
@pytest.mark.proves("a-v3-line-stops-for-a-person-and-their-answer-carries-it-on")
def test_a_line_carrying_a_wait_node_is_executable(document: bytes, sink: str) -> None:
    """A pause is admitted wherever the line puts it, at the end or in the middle.

    Both positions are stated because they end differently: an answered wait in
    the middle hands on to the heir its author declared, and an answered wait
    standing last is the node that completes the run.
    """
    parsed = parse_executable_workflow_document(document)

    assert isinstance(parsed, WorkflowGraphV3)
    assert parsed.entry_node_ids == ("implement",)
    assert parsed.sink_node_ids == (sink,)


def test_a_wait_node_may_read_a_named_predecessor_output_into_its_question() -> None:
    document = AGENT_WAIT_AGENT_CHAIN.replace(
        b"    prompt: Is this good enough to review?\n",
        b"""    prompt: Is this good enough to review?
    inputs:
      - name: candidate
        from: {node: implement, output: result}
""",
    )

    parsed = parse_executable_workflow_document(document)

    assert isinstance(parsed, WorkflowGraphV3)
    assert parsed.node("approve").inputs[0].name == "candidate"


def test_a_wait_node_may_read_a_graph_input_into_its_question() -> None:
    document = AGENT_THEN_WAIT_CHAIN.replace(
        b"name: A person answers last\n",
        b"""name: A person answers last
graph_inputs:
  - name: operator_context
    schema: {ref: any-json, revision: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}
""",
    ).replace(
        b"    prompt: Is this good enough to land?\n",
        b"""    prompt: Is this good enough to land?
    inputs:
      - name: context
        from: {graph_input: operator_context}
""",
    )

    parsed = parse_executable_workflow_document(document)

    assert isinstance(parsed, WorkflowGraphV3)
    assert parsed.node("approve").inputs[0].name == "context"


@pytest.mark.parametrize(
    ("source", "named"),
    [
        pytest.param(b'        value: "yes"\n', "value", id="authored value"),
        pytest.param(
            b"        from: {node: implement, receipt: terminal}\n",
            "receipt",
            id="node receipt",
        ),
    ],
)
def test_a_wait_input_source_nothing_composes_is_refused_by_its_form(
    source: bytes, named: str
) -> None:
    document = AGENT_WAIT_AGENT_CHAIN.replace(
        b"    prompt: Is this good enough to review?\n",
        b"    prompt: Is this good enough to review?\n"
        b"    inputs:\n"
        b"      - name: candidate\n" + source,
    )

    with pytest.raises(
        InvalidWorkflowDocument,
        match=f"input sources on wait node 'approve'.*{named}",
    ):
        parse_executable_workflow_document(document)


def test_a_wait_context_input_without_a_declared_context_is_refused_by_name() -> None:
    document = AGENT_WAIT_AGENT_CHAIN.replace(
        b"    prompt: Is this good enough to review?\n",
        b"""    prompt: Is this good enough to review?
    inputs:
      - name: candidate
        from: {context: operator_context}
""",
    )

    with pytest.raises(InvalidWorkflowDocument) as raised:
        parse_executable_workflow_document(document)

    assert raised.value.refusal is not None
    assert raised.value.refusal.reason is WorkflowRefusalReason.UNDECLARED_CONTEXT
    assert "operator_context" in raised.value.refusal.detail


# The other side of the same boundary. `depends_on` is the one authored form
# this list lost: it was refused with its siblings while nothing advanced, and it
# is admitted now because the linear rule binds it. Pinning the admission where
# the refusals live is what makes it a decision someone made rather than a hole
# that opened -- the empty authored form still means what the author wrote, and
# what it now means is "no dependency", which is exactly what an entry node is.
DECIDED_EXECUTABLE: dict[str, tuple[bytes, tuple[str, ...]]] = {
    "an empty authored depends_on": (
        ONE_AGENT_DOCUMENT + b"    depends_on: []\n",
        ("implement",),
    ),
    # `tools` joined it when an attempt began redeeming at most one exec-shaped
    # and at most one effect-shaped grant a node pins. What each grant grants
    # is the published revision's word, read where the reference is resolved;
    # the count is all this pure reading can judge, so two pins are admitted
    # here -- the finer rule that the two must differ in shape is a
    # binding-time refusal once their resolved capabilities are known.
    "one pinned tool grant": (
        ONE_AGENT_DOCUMENT
        + b"    tools: [{ref: verify, revision: %s}]\n" % (b"c" * 64),
        ("implement",),
    ),
    "two pinned tool grants": (
        ONE_AGENT_DOCUMENT
        + b"""    tools:
      - {ref: verify, revision: %s}
      - {ref: publish, revision: %s}
"""
        % (b"c" * 64, b"d" * 64),
        ("implement",),
    ),
    "an empty authored tools list": (
        ONE_AGENT_DOCUMENT + b"    tools: []\n",
        ("implement",),
    ),
    # `budget` joined it when the start began binding the pin: the published
    # revision is resolved like a schema or a tool grant, and the attempt reads
    # the turn bound those bytes named.
    "one pinned budget": (
        ONE_AGENT_DOCUMENT
        + b"    budget: {ref: build_budget, revision: %s}\n" % (b"c" * 64),
        ("implement",),
    ),
    "an authored difficulty": (
        ONE_AGENT_DOCUMENT + b"    difficulty: 3\n",
        ("implement",),
    ),
    "an authored difficulty that is the default": (
        ONE_AGENT_DOCUMENT + b"    difficulty: 2\n",
        ("implement",),
    ),
    "an authored role kind": (
        ONE_AGENT_DOCUMENT + b"    kind: review\n",
        ("implement",),
    ),
    "an authored role kind that is the default": (
        ONE_AGENT_DOCUMENT + b"    kind: build\n",
        ("implement",),
    ),
    "an authored model pin": (
        ONE_AGENT_DOCUMENT + b"    model: claude-opus-5\n",
        ("implement",),
    ),
    "an authored empty model pin": (
        ONE_AGENT_DOCUMENT + b"    model: null\n",
        ("implement",),
    ),
    "an authored family rule": (
        TWO_AGENT_CHAIN + b"    family_differs_from: builder\n",
        ("review",),
    ),
    # An Action's `body` input is read through the dependency closure ADR 0002
    # already orders, not through an immediate `depends_on` edge -- a Wait may
    # stand between the agent that produced the value and the Action that
    # reads it (#1101).
    "an action body input read through a wait": (
        AGENT_THEN_WAIT_CHAIN
        + b"""  - id: publish
    type: action
    operation: {ref: open-pr, revision: "%s"}
    depends_on: [approve]
    inputs:
      - name: body
        from: {node: implement, output: result}
"""
        % (b"e" * 64),
        ("publish",),
    ),
}


@pytest.mark.parametrize(
    ("document", "sink_node_ids"), DECIDED_EXECUTABLE.values(), ids=DECIDED_EXECUTABLE
)
@pytest.mark.proves("every-v3-shape-no-runtime-binds-is-refused-by-name")
def test_a_form_a_runtime_now_binds_is_admitted_rather_than_refused(
    document: bytes,
    sink_node_ids: tuple[str, ...],
) -> None:
    """A shape that stopped being refused says so here, beside the refusals."""
    parsed = parse_executable_workflow_document(document)

    assert isinstance(parsed, WorkflowGraphV3)
    assert parsed.entry_node_ids == ("implement",)
    assert parsed.sink_node_ids == sink_node_ids


NOT_YET_EXECUTABLE: dict[str, bytes] = {
    # An empty authored form is a statement, not an absence: the author wrote it,
    # and a start that ignored it would ignore what they wrote. `inputs: []` is
    # the exception that proves the rule -- the start binds inputs now, so an
    # empty list is a statement it obeys rather than one it drops, and it is
    # admitted below with the sources nothing binds yet still refused.
    "an empty authored skills list": ONE_AGENT_DOCUMENT + b"    skills: []\n",
    "a third tool grant on one node": ONE_AGENT_DOCUMENT
    + b"""    tools:
      - {ref: verify, revision: %s}
      - {ref: publish, revision: %s}
      - {ref: land, revision: %s}
"""
    % (b"c" * 64, b"d" * 64, b"e" * 64),
    "an empty authored context list": ONE_AGENT_DOCUMENT
    + b"    required_context: []\n",
    # The retired rule: an Action's predecessor no longer binds implicitly.
    # Every Action now declares a bound input form, or it is refused by name
    # rather than started against an edge nobody wrote as an order (#1101).
    "an action with no bound input form": ONE_AGENT_DOCUMENT
    + b"""  - id: publish
    type: action
    operation: {ref: open-pr, revision: "%s"}
    depends_on: [implement]
"""
    % (b"e" * 64),
    "an action body input naming a wait, not an agent": AGENT_THEN_WAIT_CHAIN
    + b"""  - id: publish
    type: action
    operation: {ref: open-pr, revision: "%s"}
    depends_on: [approve]
    inputs:
      - name: body
        from: {node: approve, output: result}
"""
    % (b"e" * 64),
    "a fan-out": TWO_AGENT_CHAIN
    + b"""  - id: document
    type: agent
    role: writer
    mode: headless
    instruction: Write the first thing up.
    depends_on: [implement]
"""
    + DECLARED_OUTPUT,
    "two entry nodes": ONE_AGENT_DOCUMENT
    + b"""  - id: second_entry
    type: agent
    role: other
    mode: headless
    instruction: Start independently.
"""
    + DECLARED_OUTPUT,
}


@pytest.mark.parametrize(
    ("document"), NOT_YET_EXECUTABLE.values(), ids=NOT_YET_EXECUTABLE
)
@pytest.mark.proves("every-v3-shape-no-runtime-binds-is-refused-by-name")
def test_every_v3_form_no_runtime_binds_is_refused_by_name(document: bytes) -> None:
    """A shape nothing runs is named, never started and abandoned midway."""
    with pytest.raises(InvalidWorkflowDocument, match=FORMAT_V3_NOT_EXECUTABLE):
        parse_executable_workflow_document(document)


def _with_join(rule: bytes) -> bytes:
    return TWO_AGENT_CHAIN.replace(
        b"    depends_on: [implement]\n",
        b"    depends_on: [implement]\n    join: %s\n" % rule,
    )


SCHEDULING_FORMS_NOTHING_BINDS: dict[str, tuple[bytes, str]] = {
    # `all_terminal` over a single dependency is the one authored form of ADR
    # 0006 that starts a successor on a failed upstream. Nothing here does that,
    # so an author who wrote it must read the word they wrote rather than a run
    # that ignored it and blocked anyway.
    "a join that would start a successor on a failure": (
        _with_join(b"all_terminal"),
        "join",
    ),
    # The redundant spelling of the default is refused for the same reason: an
    # admitted `all_succeeded` would be the one join that happens to match what
    # the runtime does, and the next author would read that as a bound form.
    "the default join spelled out": (_with_join(b"all_succeeded"), "join"),
    "a pinned retry policy": (
        ONE_AGENT_DOCUMENT
        + b"    retry: {ref: build_retry, revision: %s}\n" % (b"f" * 64),
        "retry",
    ),
}
"""The two forms #57 sentences 4 and 5 name, in the shape an author writes them.

Both are parse-valid and both are unbound, so the only thing standing between an
author and a run that silently ignored their sentence is that the refusal says
which sentence it ignored.
"""


@pytest.mark.parametrize(
    ("document", "form"),
    SCHEDULING_FORMS_NOTHING_BINDS.values(),
    ids=SCHEDULING_FORMS_NOTHING_BINDS,
)
@pytest.mark.proves("every-v3-shape-no-runtime-binds-is-refused-by-name")
def test_a_scheduling_form_nothing_binds_is_refused_under_its_own_word(
    document: bytes, form: str
) -> None:
    """The refusal uses the author's word, not a description of the shape.

    A run of this build applies exactly one join -- the omitted single-dependency
    `all_succeeded` -- and it applies it by never advancing past a node that did
    not succeed. Every authored join and every pinned retry policy is therefore a
    sentence nobody carries out, and the whole value of refusing it is that the
    author can find the line they wrote.
    """
    with pytest.raises(InvalidWorkflowDocument) as refused:
        parse_executable_workflow_document(document)

    assert form in str(refused.value)


ORDERED_AGENT_DOCUMENT = b"""format_version: 3
name: One agent that reads an order
graph_inputs:
  - name: order
    schema: {ref: order-schema, revision: "%s"}
nodes:
  - id: implement
    type: agent
    role: builder
    mode: headless
    instruction: Do the one thing this chain is for.
    inputs:
      - name: order
        from: {graph_input: order}
""" % (b"a" * 64)


UNBOUND_SOURCE_DOCUMENT = (
    b"""format_version: 3
name: One agent reading something nothing produces
nodes:
  - id: prepare
    type: agent
    role: builder
    mode: headless
    instruction: Go first.
"""
    + DECLARED_OUTPUT
    + b"""  - id: implement
    type: agent
    role: builder
    mode: headless
    instruction: Then me.
    depends_on: [prepare]
    inputs:
      - name: thing
        from: {node: prepare, receipt: terminal}
"""
    + DECLARED_OUTPUT
)

AUTHORED_VALUE_DOCUMENT = (
    ONE_AGENT_DOCUMENT
    + b"""    inputs:
      - name: portions
        value: "four"
"""
)


@pytest.mark.parametrize(
    ("document", "named"),
    [
        pytest.param(UNBOUND_SOURCE_DOCUMENT, "receipt", id="a node receipt"),
        pytest.param(AUTHORED_VALUE_DOCUMENT, "value", id="an authored constant"),
    ],
)
def test_an_input_reading_what_nothing_supplies_is_refused_by_its_source(
    document: bytes, named: str
) -> None:
    """Refused by the source it named, not by "inputs" as a whole.

    Only an order the graph declares has an owner at the start. A node receipt
    names something the run produces and nothing walks a V3 graph to produce it
    yet; an authored constant is the very thing an order replaces. Both are
    refused rather than admitted and quietly handed nothing.

    The other two sources -- another node's output and a context entry -- are
    refused earlier and for a different reason: a document may only read an
    output its upstream node declares, or a context entry the reading node
    requires, and `outputs` and `required_context` are themselves forms nothing
    binds yet. There is no document that reaches this refusal through them, so
    none is written here pretending otherwise.
    """
    with pytest.raises(
        InvalidWorkflowDocument, match=f"input sources nothing binds yet: {named}"
    ):
        parse_executable_workflow_document(document)


NO_OUTPUT_DOCUMENT = ONE_AGENT_DOCUMENT.replace(DECLARED_OUTPUT, b"")
TWO_OUTPUT_DOCUMENT = (
    ONE_AGENT_DOCUMENT
    + b"""      - name: notes
        schema: {ref: any-json, revision: "%s"}
"""
    % (b"e" * 64)
)


@pytest.mark.proves(
    "an-agent-node-whose-output-shape-has-no-owner-refuses-before-a-run"
)
@pytest.mark.parametrize(
    ("document", "counted"),
    [
        pytest.param(NO_OUTPUT_DOCUMENT, "0 outputs", id="no declared output"),
        pytest.param(TWO_OUTPUT_DOCUMENT, "2 outputs", id="two declared outputs"),
    ],
)
def test_an_agent_output_shape_no_runtime_enforces_is_refused_by_one_name(
    document: bytes, counted: str
) -> None:
    """Both directions of the same missing shape, refused under one name.

    `single-json-output/v1` is the one shape a run enforces: exactly one declared
    output, whose whole decoded bytes are its value. A node declaring none would
    produce bytes no schema judges -- the completion this item exists to end --
    and a node declaring several would leave one of its values answered by
    another. Neither has an owner, so both are refused before the run exists
    rather than started under a contract nothing keeps.
    """
    with pytest.raises(InvalidWorkflowDocument) as refused:
        parse_executable_workflow_document(document)

    assert AGENT_OUTPUT_SHAPE_UNAVAILABLE in str(refused.value)
    assert counted in str(refused.value)


def test_the_worked_example_of_the_record_parses_unchanged() -> None:
    record = Path(__file__).parents[2] / "docs/decisions/0006-node-vocabulary.md"
    text = record.read_text(encoding="utf-8")
    example = text.split("## Worked example", 1)[1].split("```yaml\n", 1)[1]
    example_document = example.split("```", 1)[0]

    parsed = parse_workflow_document(example_document.encode("utf-8"))

    assert isinstance(parsed, WorkflowGraphV3)
    assert parsed.entry_node_ids == ("implement",)
    assert parsed.join_of("merge_findings") == "all_terminal"
    assert tuple(entry.name for entry in parsed.graph_inputs) == ("story",)
    implement = cast(AgentNodeV3, parsed.node("implement"))
    assert tuple(entry.name for entry in implement.required_context) == ("house_rules",)
    for node_id in ("implement", "code_review", "test_review"):
        node = cast(AgentNodeV3, parsed.node(node_id))
        story = next(entry for entry in node.inputs if entry.name == "story")
        assert isinstance(story.source, GraphInputSource)
        assert story.source.graph_input == "story"
        assert all(entry.name != "story" for entry in node.required_context)
    assert "<requirement revision id>" not in example_document
