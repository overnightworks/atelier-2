"""A reader behind a loop reads the loop's last executed round, not its own.

Before this record, `implement -> review -> summarize` and
`implement -> review -> summarize -> publish` were refused: a document
reading a loop member's output from outside the loop named no round a rule
here knew how to answer. The rule now does -- a reader outside a finished
loop reads the round the loop last actually turned. `review`'s round two
accepts, having revised in round one, so what an Agent and an Action behind
the loop are handed is round two's report, never round one's.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa

from atelier2.adapters.dbos.runtime import DbosRuntime
from atelier2.adapters.dbos.schema import effect_intents, runs
from atelier2.contracts.runs import (
    FIRST_ROUND_ORDINAL,
    RunId,
    RunState,
    WorkflowRevision,
)
from atelier2.contracts.verdicts import VERDICT_ANSWER_SCHEMA, Verdict
from tests.scenarios.agents import (
    RecordingAgentExecutorFactoryV2,
    agent_scratch_root,
    answering_each_execution,
)
from tests.scenarios.durable_state import (
    canonical_loopback_effects,
    canonical_runtime_settings,
)
from tests.scenarios.runs import (
    V3_EXECUTOR_REVISION,
    V3_OPERATIONAL_IDENTITY,
    V3_PROVIDER,
    publish_pinned_revisions,
    start_published_v3_run,
)
from tests.scenarios.workflows import (
    ANY_JSON_SCHEMA,
    OPEN_PR_OPERATION,
    declared_output,
    verdict_answer,
)

RUN = RunId("v3/loop-last-round")
LOOP_ID = "until_reviewed"
LOOP_MAXIMUM_ROUNDS = 3
LOOP_ACCEPTING_ROUND = 2
CANDIDATE = b'"the candidate this round produced"'
SUMMARY = b'"the summary the reader outside the loop produced"'
REVISE_REPORT = verdict_answer(Verdict.REVISE)
ACCEPTED_REPORT = verdict_answer(Verdict.ACCEPTED)

DOCUMENT = (
    b"""format_version: 3
name: Build and review in a loop, then read its last round outside it
nodes:
  - id: implement
    type: agent
    role: builder
    mode: headless
    instruction: Do the one thing this chain is for.
"""
    + declared_output()
    + b"""  - id: review
    type: agent
    role: builder
    mode: headless
    instruction: Check what the node before you did, and say whether it is done.
    depends_on: [implement]
    inputs:
      - name: candidate
        from: {node: implement, output: result}
"""
    + declared_output(VERDICT_ANSWER_SCHEMA, "verdict")
    + b"""  - id: summarize
    type: agent
    role: builder
    mode: headless
    instruction: Summarize the loop's last review.
    depends_on: [review]
    inputs:
      - name: report
        from: {node: review, output: verdict}
"""
    + declared_output(ANY_JSON_SCHEMA, "summary")
    + f"""  - id: publish
    type: action
    operation: {{ref: open-pr, revision: {OPEN_PR_OPERATION.revision_hash.value}}}
    depends_on: [summarize]
    inputs:
      - name: body
        from: {{node: review, output: verdict}}
loops:
  - id: {LOOP_ID}
    body: [implement, review]
    maximum_rounds: {LOOP_MAXIMUM_ROUNDS}
    repeat_while: {{node: review, verdict: {Verdict.REVISE.value}}}
""".encode()
)
"""`review`'s verdict revises round one and accepts round two, so a reader
handed round one's report rather than round two's would betray itself."""

ANSWERS = (
    {
        ("implement", round_ordinal): CANDIDATE
        for round_ordinal in range(1, LOOP_MAXIMUM_ROUNDS + 1)
    }
    | {
        ("review", round_ordinal): (
            ACCEPTED_REPORT if round_ordinal == LOOP_ACCEPTING_ROUND else REVISE_REPORT
        )
        for round_ordinal in range(1, LOOP_MAXIMUM_ROUNDS + 1)
    }
    | {("summarize", FIRST_ROUND_ORDINAL): SUMMARY}
)
"""Every round each node could answer, including the rounds past acceptance:
a run that could not have answered them would prove nothing about which one
a later reader was actually handed."""


@pytest.fixture
def runtime(
    tmp_path: Path,
) -> Iterator[tuple[DbosRuntime, RecordingAgentExecutorFactoryV2]]:
    recording = RecordingAgentExecutorFactoryV2(
        V3_PROVIDER.value,
        V3_EXECUTOR_REVISION.value,
        V3_OPERATIONAL_IDENTITY,
        b"",
        command=answering_each_execution(ANSWERS),
    )
    started = DbosRuntime(
        canonical_runtime_settings(
            tmp_path, "v3-loop-last-round-test", agent_scratch_root(tmp_path)
        ),
        canonical_loopback_effects(tmp_path),
        (recording,),
    )
    started.initialize_storage()
    try:
        yield started, recording
    finally:
        started.close()


def start_and_run(runtime: DbosRuntime) -> None:
    publish_pinned_revisions(
        runtime.engine, ANY_JSON_SCHEMA, VERDICT_ANSWER_SCHEMA, OPEN_PR_OPERATION
    )
    start_published_v3_run(
        runtime.engine,
        runtime.settings,
        RUN,
        WorkflowRevision(DOCUMENT),
        runtime.agent_executor_registry,
    )
    runtime.launch()
    wait_for_state(runtime, RunState.COMPLETED)


def wait_for_state(runtime: DbosRuntime, state: RunState) -> None:
    deadline = time.monotonic() + 16
    observed = ""
    while time.monotonic() < deadline:
        with runtime.engine.connect() as connection:
            observed = str(
                connection.scalar(
                    sa.select(runs.c.state).where(runs.c.run_id == RUN.value)
                )
            )
        if observed == state.value:
            return
        time.sleep(0.025)
    raise AssertionError(f"run stayed {observed!r}, expected {state.value!r}")


def test_an_agent_behind_a_loop_reads_the_review_it_last_actually_turned(
    runtime: tuple[DbosRuntime, RecordingAgentExecutorFactoryV2],
) -> None:
    """`summarize` stands outside the loop; it is handed round two's report."""
    started_runtime, recording = runtime

    start_and_run(started_runtime)

    assert recording.opened is not None
    summarizing = next(
        request
        for request in recording.opened.requests
        if request.node_id == "summarize"
    )
    assert ACCEPTED_REPORT.decode("utf-8") in summarizing.job_bytes.decode("utf-8")
    assert REVISE_REPORT.decode("utf-8") not in summarizing.job_bytes.decode("utf-8")


def test_an_action_behind_a_loop_binds_the_review_it_last_actually_turned(
    runtime: tuple[DbosRuntime, RecordingAgentExecutorFactoryV2],
) -> None:
    """`publish` also stands outside the loop; its request body is round two's."""
    started_runtime, _recording = runtime

    start_and_run(started_runtime)

    with started_runtime.engine.connect() as connection:
        canonical_request = connection.execute(
            sa.select(effect_intents.c.canonical_request).where(
                effect_intents.c.run_id == RUN.value
            )
        ).scalar_one()
    body = json.loads(bytes(canonical_request))["body"]
    assert body == ACCEPTED_REPORT.decode("utf-8")
