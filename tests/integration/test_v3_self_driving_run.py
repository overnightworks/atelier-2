"""A V3 line drives itself from its public start to its terminal hash.

Every earlier V3 head proved one joint and held the run still around it: the
document was admitted (#194 H1a), a node bound onto the attempt path (H1c), the
terminal condition left the subworkflow node (H1b), and a node named the heir its
author declared (H2). Each was measured by calling the piece directly, because
the runtime refused the family at the start seam and no line could move on its
own.

This is the head where nothing is held. The run is started through the one public
start seam, `launch()` hands it to the real DBOS queue, and no test code touches
`start_node`, `durable_node` or the attempt store afterwards. This line said "the
public route" while a named stopgap refused a V3 revision over HTTP, which made it
false; #219 removed that refusal, so the route would answer now too. It still says
seam rather than route, because that is what this test drives -- the HTTP claim is
proven where the route is actually called. What is asserted is
what an operator would see: both nodes ran, in the order the author declared, and
the run ended on a terminal hash that recomputes from the events it wrote.
"""

from __future__ import annotations

import time
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa

from atelier2.adapters.dbos.agent_catalog import DbosAgentConfigurationCatalog
from atelier2.adapters.dbos.catalog_store import DbosCatalogStore
from atelier2.adapters.dbos.run_store import _agent_receipt_v2_from_record
from atelier2.adapters.dbos.runtime import DbosRuntime
from atelier2.adapters.dbos.schema import (
    agent_attempts,
    agent_receipts_v2,
    attempt_instants,
    event_instants,
    run_events,
    run_instants,
    runs,
)
from atelier2.adapters.dbos.starter import (
    DbosDurableRunStarter,
    DbosWorkflowRevisionPublisher,
)
from atelier2.adapters.dbos.workflow_ids import node_workflow_id_for
from atelier2.contracts.agent_attempts import AgentAttemptId, AgentAttemptState
from atelier2.contracts.agents import (
    AgentBinding,
    AgentBindingSet,
    AgentConfigurationRevision,
    AgentConfigurationRevisionFormatVersion,
    AgentExecutionCapability,
    AgentExecutorRevision,
    AgentReceiptBody,
    AgentReceiptHash,
    AgentReceiptV2,
    AgentRole,
    AuthMode,
    AuthProfileRevision,
    ProviderId,
)
from atelier2.contracts.executions import (
    NodeExecutionId,
    RunEvent,
    RunEventAgentAttemptBinding,
    RunEventKind,
    terminal_hash_for,
)
from atelier2.contracts.hashing import Sha256Hash
from atelier2.contracts.runs import (
    RunId,
    RunState,
    WorkflowRevision,
    WorkflowRevisionHash,
)
from atelier2.contracts.when import RecordedAt
from atelier2.ports.agent_configurations import (
    AgentConfigurationRevisionCreated,
    AuthProfileRevisionCreated,
)
from atelier2.ports.durable_runs import (
    DurableRunCreated,
    StartPublishedRunRequestV2,
)
from atelier2.ports.published_revisions import (
    PublishedRevisionCreated,
    PublishedRevisionExisting,
)
from atelier2.ports.run_events import RunEventPage, StreamReady
from tests.scenarios.agents import (
    RecordingAgentExecutorFactoryV2,
    agent_scratch_root,
    publish_checked_model_registry,
)
from tests.scenarios.api import durable_queries
from tests.scenarios.durable_state import (
    canonical_loopback_effects,
    canonical_runtime_settings,
)
from tests.scenarios.workflows import ANY_JSON_SCHEMA, declared_output

TWO_NODE_DOCUMENT = (
    b"""format_version: 3
name: Two agents in a line
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
    instruction: Check what the node before you did.
    depends_on: [implement]
"""
    + declared_output()
)

RUN = RunId("v3/two-agents")
PROVIDER_OUTPUT = b'"the exact provider bytes"'


@pytest.fixture
def runtime(
    tmp_path: Path,
) -> Iterator[tuple[DbosRuntime, RecordingAgentExecutorFactoryV2]]:
    """A runtime whose agent executor succeeds, so the line can actually move."""
    recording = RecordingAgentExecutorFactoryV2(
        "exact", "exact/v1", "exact-operation", PROVIDER_OUTPUT
    )
    started = DbosRuntime(
        canonical_runtime_settings(
            tmp_path, "v3-driver-test", agent_scratch_root(tmp_path)
        ),
        canonical_loopback_effects(tmp_path),
        (recording,),
    )
    started.initialize_storage()
    try:
        yield started, recording
    finally:
        started.close()


def publish_two_node_line(
    runtime: DbosRuntime,
) -> tuple[WorkflowRevision, AgentBindingSet]:
    published = DbosCatalogStore(runtime.engine).publish_revision(ANY_JSON_SCHEMA)
    assert isinstance(
        published, (PublishedRevisionCreated, PublishedRevisionExisting)
    ), published
    catalog = DbosAgentConfigurationCatalog(
        runtime.engine, runtime.agent_executor_registry
    )
    auth = AuthProfileRevision("max", 1, ProviderId("exact"), AuthMode.SUBSCRIPTION)
    assert isinstance(
        catalog.publish_auth_profile_revision(auth), AuthProfileRevisionCreated
    )
    configuration = AgentConfigurationRevision(
        "opus",
        auth.revision_hash,
        AgentExecutorRevision("exact/v1"),
        AgentExecutionCapability.HEADLESS,
        AgentConfigurationRevisionFormatVersion.V2,
    )
    assert isinstance(
        catalog.publish_agent_configuration_revision(configuration),
        AgentConfigurationRevisionCreated,
    )
    publish_checked_model_registry(
        runtime.engine, ProviderId("exact"), (configuration,)
    )
    workflow = WorkflowRevision(TWO_NODE_DOCUMENT)
    DbosWorkflowRevisionPublisher(runtime.engine).publish(workflow)
    return workflow, AgentBindingSet(
        (AgentBinding(AgentRole("builder"), configuration.revision_hash),)
    )


def wait_for_state(runtime: DbosRuntime, state: RunState) -> None:
    deadline = time.monotonic() + 8
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


@pytest.mark.proves("a-v3-run-drives-itself-through-the-runtime")
@pytest.mark.proves("a-run-carries-when-it-started-and-ended")
def test_a_v3_line_runs_both_its_nodes_without_a_hand_reaching_in(
    runtime: tuple[DbosRuntime, RecordingAgentExecutorFactoryV2],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole point of the family: started once, it finishes by itself.

    The only calls this test makes are the ones an operator makes -- publish,
    start, launch. Everything between the start and COMPLETED is the runtime's
    own chain: the bootstrap picks up the entry node, the attempt runs, the
    completion asks for the heir the author declared, and the sink ends the run.
    """
    started_runtime, recording = runtime
    workflow, bindings = publish_two_node_line(started_runtime)
    first_transition = RecordedAt("2026-08-18T15:00:01Z")
    terminal_transition = RecordedAt("2026-08-18T15:00:02Z")
    transition_clock = iter((first_transition, terminal_transition))
    monkeypatch.setattr(
        "atelier2.adapters.dbos.run_transitions.recorded_instant",
        lambda now=None: next(transition_clock),
    )

    started = DbosDurableRunStarter(
        started_runtime.engine,
        started_runtime.settings,
        started_runtime.agent_executor_registry,
    ).start_published(StartPublishedRunRequestV2(RUN, workflow.revision_hash, bindings))
    assert isinstance(started, DurableRunCreated)

    started_runtime.launch()
    wait_for_state(started_runtime, RunState.COMPLETED)

    with started_runtime.engine.connect() as connection:
        run = (
            connection.execute(sa.select(runs).where(runs.c.run_id == RUN.value))
            .mappings()
            .one()
        )
        events = [
            (
                int(str(record["event_sequence"])),
                str(record["node_id"]),
                str(record["event_kind"]),
                bytes(record["payload"]),
            )
            for record in connection.execute(
                sa.select(run_events)
                .where(run_events.c.run_id == RUN.value)
                .order_by(run_events.c.event_sequence)
            ).mappings()
        ]
        attempts = [
            (str(record["node_id"]), str(record["state"]))
            for record in connection.execute(
                sa.select(agent_attempts)
                .where(agent_attempts.c.run_id == RUN.value)
                .order_by(agent_attempts.c.node_id)
            ).mappings()
        ]
        event_hashes = tuple(
            Sha256Hash(str(value))
            for value in connection.execute(
                sa.select(run_events.c.event_hash)
                .where(run_events.c.run_id == RUN.value)
                .order_by(run_events.c.event_sequence)
            ).scalars()
        )
        receipt_count = connection.scalar(
            sa.select(sa.func.count()).select_from(agent_receipts_v2)
        )
        node_workflows = tuple(
            connection.execute(
                sa.text(
                    "SELECT workflow_uuid FROM workflow_status "
                    "WHERE name='atelier2_graph_node' ORDER BY workflow_uuid"
                )
            ).scalars()
        )
        instant = (
            connection.execute(
                sa.select(run_instants).where(run_instants.c.run_id == RUN.value)
            )
            .mappings()
            .one()
        )
        RecordedAt(str(instant["started_at"]))
        RecordedAt(str(instant["ended_at"]))
        assert str(instant["ended_at"]) == terminal_transition.value
        assert str(instant["ended_at"]) != first_transition.value
        for record in connection.execute(
            sa.select(attempt_instants)
            .select_from(
                attempt_instants.join(
                    agent_attempts,
                    attempt_instants.c.attempt_id == agent_attempts.c.attempt_id,
                )
            )
            .where(agent_attempts.c.run_id == RUN.value)
        ).mappings():
            RecordedAt(str(record["started_at"]))
            RecordedAt(str(record["ended_at"]))
        event_times = tuple(
            RecordedAt(str(value))
            for value in connection.execute(
                sa.select(event_instants.c.recorded_at)
                .where(event_instants.c.run_id == RUN.value)
                .order_by(event_instants.c.event_sequence)
            ).scalars()
        )
        assert event_times == (first_transition, terminal_transition)

    assert events == [
        (1, "implement", RunEventKind.AGENT_COMPLETED.value, PROVIDER_OUTPUT),
        (2, "review", RunEventKind.AGENT_COMPLETED.value, PROVIDER_OUTPUT),
    ]
    assert attempts == [
        ("implement", AgentAttemptState.SUCCEEDED.value),
        ("review", AgentAttemptState.SUCCEEDED.value),
    ]
    assert recording.opened is not None
    assert [request.node_id for request in recording.opened.requests] == [
        "implement",
        "review",
    ]
    assert receipt_count == 2
    assert node_workflows == tuple(
        sorted(
            node_workflow_id_for(
                NodeExecutionId.for_node(RUN, workflow.revision_hash, node_id)
            )
            for node_id in ("implement", "review")
        )
    )
    assert str(run["current_node_id"]) == "review"
    assert int(str(run["workflow_format_version"])) == 3
    assert (
        str(run["terminal_hash"])
        == terminal_hash_for(workflow.revision_hash, event_hashes).value
    )


def _receipt_hash_a_verifier_recomputes(
    receipt: AgentReceiptV2, *, model: str
) -> AgentReceiptHash:
    """The receipt hash folded out of the fields a verifier was handed.

    Every dimension comes from the presented receipt except the model, which the
    caller states -- that is the one binding this test tampers with to see
    whether the chain notices.
    """
    return AgentReceiptBody(
        receipt.request_hash,
        receipt.node_execution_id,
        receipt.run_id,
        receipt.workflow_revision_hash,
        receipt.node_id,
        receipt.role,
        receipt.binding_set_hash,
        receipt.agent_configuration_revision_hash,
        receipt.auth_profile_revision_hash,
        receipt.profile_id,
        receipt.revision_number,
        receipt.provider_id,
        receipt.auth_mode,
        model,
        receipt.executor_revision,
        receipt.executor_operational_identity,
        receipt.output_bytes,
        receipt.output_hash,
    ).fingerprint()


def _terminal_hash_a_verifier_recomputes(
    revision_hash: WorkflowRevisionHash,
    events: Sequence[Mapping[str, Any]],
    receipt_hashes: Mapping[str, AgentReceiptHash],
) -> Sha256Hash:
    """Fold the chain out of presented fields, reading no hash the store wrote."""
    rebuilt = tuple(
        RunEvent(
            RunId(str(record["run_id"])),
            revision_hash,
            int(record["event_sequence"]),
            str(record["node_id"]),
            NodeExecutionId.for_node(
                RunId(str(record["run_id"])), revision_hash, str(record["node_id"])
            ),
            RunEventKind(str(record["event_kind"])),
            bytes(record["payload"]),
            attempt_binding=RunEventAgentAttemptBinding(
                AgentAttemptId(str(record["agent_attempt_id"])),
                int(record["attempt_ordinal"]),
            ),
            agent_receipt_hash=receipt_hashes[str(record["node_id"])],
        )
        for record in events
    )
    return terminal_hash_for(
        revision_hash, tuple(event.event_hash for event in rebuilt)
    )


@pytest.mark.proves("a-terminal-hash-proves-the-binding-its-run-worked-under")
def test_the_terminal_hash_recomputes_only_under_the_binding_the_run_had(
    runtime: tuple[DbosRuntime, RecordingAgentExecutorFactoryV2],
) -> None:
    """Whoever recomputes the terminal hash also proves under which binding it ran.

    The recomputation below reads fields, never the hashes the store wrote: the
    receipt hash is folded out of the presented receipt and the event hashes out
    of the presented events. Handing the same fold a receipt whose model differs
    by one string has to miss the terminal hash -- otherwise the chain would be
    proving the ordered output bytes and nothing about the provider, profile,
    model or executor that produced them.
    """
    started, _ = runtime
    workflow, bindings = publish_two_node_line(started)
    DbosDurableRunStarter(
        started.engine,
        started.settings,
        started.agent_executor_registry,
    ).start_published(StartPublishedRunRequestV2(RUN, workflow.revision_hash, bindings))
    started.launch()
    wait_for_state(started, RunState.COMPLETED)

    with started.engine.connect() as connection:
        terminal_hash = str(
            connection.scalar(
                sa.select(runs.c.terminal_hash).where(runs.c.run_id == RUN.value)
            )
        )
        events = [
            dict(record)
            for record in connection.execute(
                sa.select(run_events)
                .where(run_events.c.run_id == RUN.value)
                .order_by(run_events.c.event_sequence)
            ).mappings()
        ]
        receipts = [
            _agent_receipt_v2_from_record(record)
            for record in connection.execute(sa.select(agent_receipts_v2)).mappings()
        ]

    honest = {
        receipt.node_id: _receipt_hash_a_verifier_recomputes(
            receipt, model=receipt.model
        )
        for receipt in receipts
    }
    tampered = {
        receipt.node_id: _receipt_hash_a_verifier_recomputes(
            receipt, model="a model this run never ran under"
        )
        for receipt in receipts
    }

    assert [str(event["agent_receipt_hash"]) for event in events] == [
        honest[str(event["node_id"])].value for event in events
    ]
    assert (
        _terminal_hash_a_verifier_recomputes(
            workflow.revision_hash, events, honest
        ).value
        == terminal_hash
    )
    assert (
        _terminal_hash_a_verifier_recomputes(
            workflow.revision_hash, events, tampered
        ).value
        != terminal_hash
    )


def test_the_finished_line_can_have_its_events_read_back(
    runtime: tuple[DbosRuntime, RecordingAgentExecutorFactoryV2],
) -> None:
    """A run that ended is a run whose history opens, not one reported corrupt.

    The stream's pre-flight decides whether the head event is the one that ended
    the run. It knew a single spelling of an ending -- the subworkflow completion
    a V1 or V2 line ends on -- and a V3 line ends on its **agent** sink, because
    #194 H1b lifted the terminal condition off the subworkflow node onto the run.
    So every finished V3 run answered `durable-state-corrupt`: the loudest thing
    this system can say, about a store that is intact.

    Measured on the operator's own first V3 run before this repair, where the run
    itself read back at 200 while its events answered 500. The cockpit opens this
    stream for the run it is showing, so the page rendered and its live view fell
    straight to disconnected.
    """
    started, _ = runtime
    workflow, bindings = publish_two_node_line(started)
    DbosDurableRunStarter(
        started.engine,
        started.settings,
        started.agent_executor_registry,
    ).start_published(StartPublishedRunRequestV2(RUN, workflow.revision_hash, bindings))
    started.launch()
    wait_for_state(started, RunState.COMPLETED)

    queries = durable_queries(started.engine)
    prepared = queries.prepare_run_event_stream(RUN, 0)
    page = queries.read_run_event_page(RUN, 0, 50)

    assert isinstance(prepared, StreamReady), prepared
    assert prepared.terminal is True
    assert prepared.head_sequence == 2
    # The pre-flight only buys the 200 and the stream headers; this page is what
    # carries the events to the browser, so a real finished line is read here
    # rather than only admitted one call earlier.
    assert isinstance(page, RunEventPage), page
    assert page.terminal_seen is True
    assert [event.event.node_id for event in page.events] == ["implement", "review"]
