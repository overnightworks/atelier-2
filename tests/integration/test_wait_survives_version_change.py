"""A run parked on a Wait outlives the `application_version` it was started under.

**Why this file exists.** The auto-redeploy watcher (#1027) counted
`WAITING_INPUT` as active work and deferred every deploy behind an open
conversation, because a redeploy hands the new serve a fresh
`--application-version` and DBOS recovers a workflow only under the version that
enqueued it. That reasoning holds for a *running* workflow. A parked Wait is not
one: the node workflow that wrote `WAITING_INPUT` has already returned, and the
answer door enqueues the continuation itself, under the answering runtime's own
version (`DbosWaitAnswerer`). This test is the proof that the two facts add up --
without it the watcher's rule rests on an assumption nobody drove.

Two Waits rather than one, because a run that merely ends after its answer
cannot show where the *continuation* was attributed. The second Wait's own node
workflow is that continuation, and this test reads the version DBOS recorded for
every workflow the run minted: it fails if anything after the redeploy were
carried under the version that parked the first Wait.

The run keeps no bound agent: a document of Waits alone parks on a person with
no executor alive, so nothing but the version change can explain a stranded
answer.
"""

from __future__ import annotations

import base64
import time
from collections import Counter
from pathlib import Path

import sqlalchemy as sa
from fastapi.testclient import TestClient
from httpx import Response

from atelier2.adapters.dbos.names import (
    ANSWER_WORKFLOW_NAME,
    NODE_WORKFLOW_NAME,
    WORKFLOW_NAME,
)
from atelier2.adapters.dbos.runtime import DbosRuntime
from atelier2.adapters.dbos.schema import runs
from atelier2.api.references import encode_public_run_reference
from atelier2.contracts.executions import NodeExecutionId
from atelier2.contracts.runs import (
    FIRST_ROUND_ORDINAL,
    RunId,
    RunState,
    WorkflowRevision,
)
from tests.scenarios.agents import agent_scratch_root
from tests.scenarios.api import durable_api_client
from tests.scenarios.durable_state import (
    canonical_loopback_effects,
    canonical_runtime_settings,
)
from tests.scenarios.runs import (
    NO_AGENT_EXECUTORS,
    publish_pinned_revisions,
    start_published_v3_run,
)
from tests.scenarios.workflows import ANY_JSON_SCHEMA, declared_output

RUN = RunId("wait/survives-a-version-change")
PARKED_NODE = "approve"
CONTINUATION_NODE = "confirm"

TWO_WAITS_DOCUMENT = (
    b"""format_version: 3
name: A person answers twice, and a redeploy happens in between
nodes:
  - id: """
    + PARKED_NODE.encode()
    + b"""
    type: wait
    prompt: Approve this line.
"""
    + declared_output(ANY_JSON_SCHEMA, "approval")
    + b"""  - id: """
    + CONTINUATION_NODE.encode()
    + b"""
    type: wait
    prompt: Confirm what you approved.
    depends_on: ["""
    + PARKED_NODE.encode()
    + b"""]
"""
    + declared_output(ANY_JSON_SCHEMA, "confirmation")
)
"""The smallest document with a node *after* the Wait a redeploy interrupts."""

WORKFLOW = WorkflowRevision(TWO_WAITS_DOCUMENT)
ANSWER = b'"approved after the redeploy"'
VERSION_THAT_PARKED_THE_WAIT = "executor-A"
VERSION_THAT_TAKES_THE_ANSWER = "executor-B"
STATE_TIMEOUT_SECONDS = 12.0
STATE_POLL_SECONDS = 0.025

# DBOS owns this table and the version it stamps on every workflow it mints;
# `atelier2.adapters.dbos.uncontinuable_runs` is the production reader that
# scopes recovery by exactly this column.
dbos_workflow_status = sa.table(
    "workflow_status",
    sa.column("name"),
    sa.column("application_version"),
)


def runtime_over(root: Path, application_version: str) -> DbosRuntime:
    """A runtime over the durable state in this directory, as a redeploy builds one."""
    return DbosRuntime(
        canonical_runtime_settings(root, application_version, agent_scratch_root(root)),
        canonical_loopback_effects(root),
        (),
    )


def run_state(runtime: DbosRuntime) -> tuple[str, str]:
    with runtime.engine.connect() as connection:
        record = connection.execute(
            sa.select(runs.c.state, runs.c.current_node_id).where(
                runs.c.run_id == RUN.value
            )
        ).one()
    return str(record.state), str(record.current_node_id)


def wait_until_run_stands_at(runtime: DbosRuntime, expected: tuple[str, str]) -> None:
    deadline = time.monotonic() + STATE_TIMEOUT_SECONDS
    observed = ("", "")
    while time.monotonic() < deadline:
        observed = run_state(runtime)
        if observed == expected:
            return
        time.sleep(STATE_POLL_SECONDS)
    raise AssertionError(f"run stayed {observed!r}, expected {expected!r}")


def minted_workflow_versions(runtime: DbosRuntime) -> Counter[tuple[str, str]]:
    """How many workflows of each name DBOS minted under each application version."""
    with runtime.engine.connect() as connection:
        return Counter(
            (str(record.name), str(record.application_version))
            for record in connection.execute(sa.select(dbos_workflow_status))
        )


def answer_through_the_public_door(client: TestClient, node_id: str) -> Response:
    execution_id = NodeExecutionId.for_node(
        RUN, WORKFLOW.revision_hash, node_id, FIRST_ROUND_ORDINAL
    )
    return client.post(
        f"/atelier/api/v1/runs/{encode_public_run_reference(RUN)}/answers",
        json={
            "workflow_revision_hash": WORKFLOW.revision_hash.value,
            "node_id": node_id,
            "expected_node_execution_id": execution_id.value,
            "actor": "operator",
            "answer_base64": base64.b64encode(ANSWER).decode("ascii"),
        },
    )


def test_a_parked_wait_takes_its_answer_under_a_later_application_version(
    tmp_path: Path,
) -> None:
    parked = runtime_over(tmp_path, VERSION_THAT_PARKED_THE_WAIT)
    parked.initialize_storage()
    try:
        publish_pinned_revisions(parked.engine, ANY_JSON_SCHEMA)
        start_published_v3_run(
            parked.engine, parked.settings, RUN, WORKFLOW, NO_AGENT_EXECUTORS, roles=()
        )
        parked.launch()
        wait_until_run_stands_at(parked, (RunState.WAITING_INPUT.value, PARKED_NODE))
    finally:
        parked.close()

    # The redeploy: the same durable store, a new process, a new version. The
    # answer door is the only thing that moves this run from here on.
    redeployed = runtime_over(tmp_path, VERSION_THAT_TAKES_THE_ANSWER)
    try:
        redeployed.launch()
        client = durable_api_client(redeployed)

        read_back = client.get(
            f"/atelier/api/v1/runs/{encode_public_run_reference(RUN)}"
        )
        assert read_back.status_code == 200, read_back.text
        assert read_back.json()["state"] == RunState.WAITING_INPUT.value

        accepted = answer_through_the_public_door(client, PARKED_NODE)
        assert accepted.status_code == 202, accepted.text

        # The successor Wait pausing is the continuation running: the answer
        # moved the run on rather than merely ending it.
        wait_until_run_stands_at(
            redeployed, (RunState.WAITING_INPUT.value, CONTINUATION_NODE)
        )
        confirmed = answer_through_the_public_door(client, CONTINUATION_NODE)
        assert confirmed.status_code == 202, confirmed.text

        wait_until_run_stands_at(
            redeployed, (RunState.COMPLETED.value, CONTINUATION_NODE)
        )
        proceeded = client.get(
            f"/atelier/api/v1/runs/{encode_public_run_reference(RUN)}"
        )
        assert proceeded.status_code == 200, proceeded.text
        assert proceeded.json()["state"] == RunState.COMPLETED.value

        # Everything before the redeploy is attributed to the version that
        # parked the Wait; every workflow after it -- both answers and the
        # continuation node -- to the version that took the answer. A
        # continuation attributed to the retired version is the stranding this
        # file exists to rule out, and it would fail exactly here.
        assert minted_workflow_versions(redeployed) == Counter(
            {
                (WORKFLOW_NAME, VERSION_THAT_PARKED_THE_WAIT): 1,
                (NODE_WORKFLOW_NAME, VERSION_THAT_PARKED_THE_WAIT): 1,
                (ANSWER_WORKFLOW_NAME, VERSION_THAT_TAKES_THE_ANSWER): 2,
                (NODE_WORKFLOW_NAME, VERSION_THAT_TAKES_THE_ANSWER): 1,
            }
        )
    finally:
        redeployed.close()
