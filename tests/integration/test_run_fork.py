from __future__ import annotations

import time
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import OperationalError

from atelier2.adapters.dbos.catalog_store import DbosCatalogStore
from atelier2.adapters.dbos.runtime import DbosRuntime
from atelier2.adapters.dbos.schema import (
    agent_attempts,
    effect_intents,
    node_execution_requests_v3,
    node_receipts_v3,
    run_agent_bindings,
    run_configuration_revisions,
    run_events,
    run_fork_reused_nodes,
    run_forks,
    run_inputs_v3,
    runs,
)
from atelier2.adapters.dbos.starter import (
    DbosDurableRunStarter,
    DbosWorkflowRevisionPublisher,
)
from atelier2.adapters.yaml_workflows import parse_workflow_document
from atelier2.api.openapi import API_PREFIX
from atelier2.api.references import encode_public_run_reference
from atelier2.application.evaluate_executability import (
    ExecutableDocument,
    resolve_document_references,
)
from atelier2.contracts.adapter_operations_v3 import AdapterOperationName
from atelier2.contracts.agents import AgentBindingSetHash, AgentExecutionCapability
from atelier2.contracts.executions import NodeExecutionId
from atelier2.contracts.run_configuration_v3 import RunConfigurationRevision
from atelier2.contracts.run_forks import (
    MAXIMUM_RUN_FORK_SUCCESSORS,
    RunForkCommandId,
    successor_run_id_for,
)
from atelier2.contracts.runs import (
    RunId,
    RunState,
    WorkflowRevision,
    WorkflowRevisionHash,
)
from atelier2.contracts.workflows_v3 import WorkflowGraphV3
from atelier2.ports.agent_executions import AgentExecutorRegistry
from atelier2.ports.durable_run_forks import (
    DurableRunForkCapabilityUnavailable,
    DurableRunForkCommandConflict,
    DurableRunForkCreated,
    DurableRunForkDocumentNotExecutable,
    DurableRunForkExecutorUnavailable,
    DurableRunForkExisting,
    DurableRunForkLoopUnsupported,
    DurableRunForkNodeMissing,
    DurableRunForkOriginMissing,
    DurableRunForkOriginNotTerminal,
    DurableRunForkPrefixNotReusable,
    DurableRunForkStateCorrupt,
    DurableRunForkWriteUnavailable,
    ForkRunRequest,
)
from atelier2.ports.durable_runs import (
    DurableRunCreated,
    StartPublishedRunRequestV2,
    StartPublishedRunRequestV3,
)
from atelier2.ports.workflow_revisions import ProjectionTooLarge
from tests.integration.test_v3_ordered_run import (
    PORTIONS_SCHEMA,
    order,
    publish_ordered_workflow,
)
from tests.integration.test_v3_self_driving_run import (
    PROVIDER_OUTPUT,
    RUN,
    RecordingAgentExecutorFactoryV2,
    publish_two_node_line,
)
from tests.integration.test_v3_wait_run import (
    ANSWER as WAIT_ANSWER,
)
from tests.integration.test_v3_wait_run import (
    RUN as WAIT_RUN,
)
from tests.integration.test_v3_wait_run import (
    WAIT_IN_THE_MIDDLE,
)
from tests.integration.test_v3_wait_run import (
    answer as answer_wait,
)
from tests.integration.test_v3_wait_run import (
    start_and_launch as start_and_launch_wait,
)
from tests.scenarios.agents import agent_scratch_root
from tests.scenarios.api import durable_api_client, durable_queries
from tests.scenarios.durable_state import (
    canonical_loopback_effects,
    canonical_runtime_settings,
)
from tests.scenarios.publishing_runs import (
    BUILD,
    Publisher,
    push_grant,
    push_operation,
    workflow_document,
)
from tests.scenarios.runs import publish_pinned_revisions
from tests.scenarios.workflows import LOOPED_LINE_DOCUMENT


def _wait_for_run(runtime: DbosRuntime, run_id: RunId, state: RunState) -> None:
    deadline = time.monotonic() + 8
    observed: str | None = None
    while time.monotonic() < deadline:
        with runtime.engine.connect() as connection:
            observed = connection.scalar(
                sa.select(runs.c.state).where(runs.c.run_id == run_id.value)
            )
        if observed == state.value:
            return
        time.sleep(0.025)
    raise AssertionError(f"run {run_id.value!r} stayed {observed!r}")


def _runtime(
    tmp_path: Path, output: bytes = PROVIDER_OUTPUT
) -> tuple[DbosRuntime, RecordingAgentExecutorFactoryV2]:
    recording = RecordingAgentExecutorFactoryV2(
        "exact", "exact/v1", "exact-operation", output
    )
    runtime = DbosRuntime(
        canonical_runtime_settings(
            tmp_path, "run-fork-test", agent_scratch_root(tmp_path)
        ),
        canonical_loopback_effects(tmp_path),
        (recording,),
    )
    runtime.initialize_storage()
    return runtime, recording


def _completed_origin(
    runtime: DbosRuntime,
) -> tuple[DbosDurableRunStarter, object]:
    workflow, bindings = publish_two_node_line(runtime)
    starter = DbosDurableRunStarter(
        runtime.engine, runtime.settings, runtime.agent_executor_registry
    )
    started = starter.start_published(
        StartPublishedRunRequestV2(RUN, workflow.revision_hash, bindings)
    )
    assert isinstance(started, DurableRunCreated)
    runtime.launch()
    _wait_for_run(runtime, RUN, RunState.COMPLETED)
    return starter, workflow


def test_fork_reuses_only_the_strict_prefix_and_executes_from_the_target(
    tmp_path: Path,
) -> None:
    runtime, recording = _runtime(tmp_path)
    try:
        starter, _workflow = _completed_origin(runtime)

        request = ForkRunRequest(RUN, "retry-review", "review")
        created = starter.fork_run(request)
        assert isinstance(created, DurableRunForkCreated)
        successor = successor_run_id_for(
            RunForkCommandId.for_request(RUN, "retry-review")
        )
        assert created.run.run_id == successor
        assert tuple(entry.node_id for entry in created.fork.reused_nodes) == (
            "implement",
        )
        _wait_for_run(runtime, successor, RunState.COMPLETED)

        retried = starter.fork_run(request)
        assert isinstance(retried, DurableRunForkExisting)
        assert retried.fork == created.fork
        assert isinstance(
            starter.fork_run(ForkRunRequest(RUN, "retry-review", "implement")),
            DurableRunForkCommandConflict,
        )
        successor_execution_id = NodeExecutionId.for_node(
            successor, created.run.revision_hash, "review"
        )
        with runtime.engine.connect() as connection:
            assert (
                connection.scalar(
                    sa.select(sa.func.count()).select_from(run_fork_reused_nodes)
                )
                == 1
            )
            assert (
                connection.scalar(
                    sa.select(sa.func.count())
                    .select_from(run_events)
                    .where(run_events.c.run_id == successor.value)
                )
                == 1
            )
            assert (
                connection.scalar(
                    sa.select(sa.func.count())
                    .select_from(agent_attempts)
                    .where(agent_attempts.c.run_id == successor.value)
                )
                == 1
            )
            assert (
                connection.scalar(
                    sa.select(sa.func.count())
                    .select_from(node_execution_requests_v3)
                    .where(
                        node_execution_requests_v3.c.node_execution_id
                        == successor_execution_id.value
                    )
                )
                == 1
            )
            assert (
                connection.scalar(
                    sa.select(sa.func.count())
                    .select_from(node_receipts_v3)
                    .where(
                        node_receipts_v3.c.node_execution_id
                        == successor_execution_id.value
                    )
                )
                == 1
            )
        assert recording.opened is not None
        assert [request.node_id for request in recording.opened.requests] == [
            "implement",
            "review",
            "review",
        ]

        api = durable_api_client(runtime)
        listed = api.get(API_PREFIX + "/runs?limit=50")
        detail = api.get(API_PREFIX + "/runs/" + encode_public_run_reference(successor))
        origin_detail = api.get(
            API_PREFIX + "/runs/" + encode_public_run_reference(RUN)
        )
        assert (
            listed.status_code == detail.status_code == origin_detail.status_code == 200
        )
        listed_by_id = {
            item["run"]["run_id"]: item["run"] for item in listed.json()["items"]
        }
        assert listed_by_id[successor.value] == detail.json()
        assert listed_by_id[RUN.value] == origin_detail.json()
        assert detail.json()["node_rail"][0]["reused_from_run_reference"] == (
            encode_public_run_reference(RUN)
        )
        assert detail.json()["fork_origin"]["public_run_reference"] == (
            encode_public_run_reference(RUN)
        )
        assert origin_detail.json()["fork_successors"] == [
            {
                "public_run_reference": encode_public_run_reference(successor),
                "restart_from_node_id": "review",
                "fork_hash": created.fork.fork_hash.value,
            }
        ]
    finally:
        runtime.close()


def test_fork_reuses_a_successfully_answered_wait_before_the_target(
    tmp_path: Path,
) -> None:
    runtime, _recording = _runtime(tmp_path)
    try:
        workflow = start_and_launch_wait(runtime, WAIT_IN_THE_MIDDLE)
        _wait_for_run(runtime, WAIT_RUN, RunState.WAITING_INPUT)
        answer_wait(runtime, workflow, WAIT_ANSWER)
        _wait_for_run(runtime, WAIT_RUN, RunState.COMPLETED)

        created = DbosDurableRunStarter(
            runtime.engine, runtime.settings, runtime.agent_executor_registry
        ).fork_run(ForkRunRequest(WAIT_RUN, "reuse-wait", "review"))

        assert isinstance(created, DurableRunForkCreated)
        assert tuple(entry.node_id for entry in created.fork.reused_nodes) == (
            "implement",
            "approve",
        )
        _wait_for_run(runtime, created.run.run_id, RunState.COMPLETED)
    finally:
        runtime.close()


def test_fork_refuses_a_missing_or_nonterminal_origin_without_creating_rows(
    tmp_path: Path,
) -> None:
    runtime, _recording = _runtime(tmp_path)
    try:
        workflow, bindings = publish_two_node_line(runtime)
        starter = DbosDurableRunStarter(
            runtime.engine, runtime.settings, runtime.agent_executor_registry
        )
        assert isinstance(
            starter.fork_run(ForkRunRequest(RunId("missing"), "key", "implement")),
            DurableRunForkOriginMissing,
        )
        started = starter.start_published(
            StartPublishedRunRequestV2(RUN, workflow.revision_hash, bindings)
        )
        assert isinstance(started, DurableRunCreated)
        assert isinstance(
            starter.fork_run(ForkRunRequest(RUN, "key", "implement")),
            DurableRunForkOriginNotTerminal,
        )
        with runtime.engine.connect() as connection:
            assert (
                connection.scalar(sa.select(sa.func.count()).select_from(run_forks))
                == 0
            )
    finally:
        runtime.close()


def test_fork_refuses_when_the_durable_executor_is_missing_or_lacks_capability(
    tmp_path: Path,
) -> None:
    runtime, recording = _runtime(tmp_path)
    try:
        _completed_origin(runtime)
        missing = DbosDurableRunStarter(
            runtime.engine, runtime.settings, AgentExecutorRegistry()
        )
        assert isinstance(
            missing.fork_run(ForkRunRequest(RUN, "missing-executor", "implement")),
            DurableRunForkExecutorUnavailable,
        )

        incompatible = RecordingAgentExecutorFactoryV2(
            recording.provider,
            recording.revision,
            recording.operational_identity_value,
            recording.output,
            capability_set=frozenset({AgentExecutionCapability.HEADLESS_WITH_TOOLS}),
        )
        incapable = DbosDurableRunStarter(
            runtime.engine,
            runtime.settings,
            AgentExecutorRegistry((incompatible,)),
        )
        assert isinstance(
            incapable.fork_run(ForkRunRequest(RUN, "missing-capability", "implement")),
            DurableRunForkCapabilityUnavailable,
        )
        with runtime.engine.connect() as connection:
            assert (
                connection.scalar(sa.select(sa.func.count()).select_from(run_forks))
                == 0
            )
    finally:
        runtime.close()


def test_fork_copies_exact_bindings_and_orders_without_changing_the_origin(
    tmp_path: Path,
) -> None:
    runtime, _recording = _runtime(tmp_path, b'"cooked"')
    origin = RunId("ordered-origin")
    try:
        workflow, bindings = publish_ordered_workflow(runtime, PORTIONS_SCHEMA)
        starter = DbosDurableRunStarter(
            runtime.engine, runtime.settings, runtime.agent_executor_registry
        )
        started = starter.start_published(
            StartPublishedRunRequestV3(
                origin,
                workflow.revision_hash,
                bindings,
                run_inputs=(order(b'{"portions": 4}', PORTIONS_SCHEMA),),
            )
        )
        assert isinstance(started, DurableRunCreated)
        runtime.launch()
        _wait_for_run(runtime, origin, RunState.COMPLETED)
        with runtime.engine.connect() as connection:
            origin_bindings = tuple(
                dict(row)
                for row in connection.execute(
                    sa.select(run_agent_bindings).where(
                        run_agent_bindings.c.run_id == origin.value
                    )
                ).mappings()
            )
            origin_orders = tuple(
                dict(row)
                for row in connection.execute(
                    sa.select(run_inputs_v3).where(
                        run_inputs_v3.c.run_id == origin.value
                    )
                ).mappings()
            )

        created = starter.fork_run(ForkRunRequest(origin, "copy", "cook"))
        assert isinstance(created, DurableRunForkCreated)
        with runtime.engine.connect() as connection:
            after_origin_bindings = tuple(
                dict(row)
                for row in connection.execute(
                    sa.select(run_agent_bindings).where(
                        run_agent_bindings.c.run_id == origin.value
                    )
                ).mappings()
            )
            after_origin_orders = tuple(
                dict(row)
                for row in connection.execute(
                    sa.select(run_inputs_v3).where(
                        run_inputs_v3.c.run_id == origin.value
                    )
                ).mappings()
            )
            successor_bindings = tuple(
                dict(row)
                for row in connection.execute(
                    sa.select(run_agent_bindings).where(
                        run_agent_bindings.c.run_id == created.run.run_id.value
                    )
                ).mappings()
            )
            successor_orders = tuple(
                dict(row)
                for row in connection.execute(
                    sa.select(run_inputs_v3).where(
                        run_inputs_v3.c.run_id == created.run.run_id.value
                    )
                ).mappings()
            )

        assert after_origin_bindings == origin_bindings
        assert after_origin_orders == origin_orders
        assert [{**row, "run_id": origin.value} for row in successor_bindings] == list(
            origin_bindings
        )
        assert [{**row, "run_id": origin.value} for row in successor_orders] == list(
            origin_orders
        )
    finally:
        runtime.close()


def test_enqueue_failure_rolls_back_the_whole_fork_transaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, _recording = _runtime(tmp_path)
    try:
        starter, _workflow = _completed_origin(runtime)
        command_id = RunForkCommandId.for_request(RUN, "failed-enqueue")
        successor = successor_run_id_for(command_id)

        def fail_enqueue(*_args: object, **_kwargs: object) -> None:
            raise OperationalError("enqueue", {}, RuntimeError("busy"))

        monkeypatch.setattr(
            "atelier2.adapters.dbos.run_fork_store.DBOSClient.enqueue_in_transaction",
            fail_enqueue,
        )
        result = starter.fork_run(ForkRunRequest(RUN, "failed-enqueue", "review"))

        assert isinstance(result, DurableRunForkWriteUnavailable)
        with runtime.engine.connect() as connection:
            assert (
                connection.scalar(sa.select(sa.func.count()).select_from(run_forks))
                == 0
            )
            assert (
                connection.scalar(
                    sa.select(sa.func.count())
                    .select_from(runs)
                    .where(runs.c.run_id == successor.value)
                )
                == 0
            )
    finally:
        runtime.close()


def test_fork_refuses_a_missing_node_and_an_unreusable_failed_prefix(
    tmp_path: Path,
) -> None:
    runtime, _recording = _runtime(tmp_path, b"not-json")
    try:
        workflow, bindings = publish_two_node_line(runtime)
        starter = DbosDurableRunStarter(
            runtime.engine, runtime.settings, runtime.agent_executor_registry
        )
        started = starter.start_published(
            StartPublishedRunRequestV2(RUN, workflow.revision_hash, bindings)
        )
        assert isinstance(started, DurableRunCreated)
        runtime.launch()
        _wait_for_run(runtime, RUN, RunState.FAILED)

        assert isinstance(
            starter.fork_run(ForkRunRequest(RUN, "missing-node", "absent")),
            DurableRunForkNodeMissing,
        )
        assert isinstance(
            starter.fork_run(ForkRunRequest(RUN, "failed-prefix", "review")),
            DurableRunForkPrefixNotReusable,
        )
    finally:
        runtime.close()


def test_fork_refuses_a_loop_instead_of_guessing_its_round(
    tmp_path: Path,
) -> None:
    runtime, _recording = _runtime(tmp_path)
    try:
        _line, bindings = publish_two_node_line(runtime)
        looped = WorkflowRevision(LOOPED_LINE_DOCUMENT)
        DbosWorkflowRevisionPublisher(runtime.engine).publish(looped)
        starter = DbosDurableRunStarter(
            runtime.engine, runtime.settings, runtime.agent_executor_registry
        )
        started = starter.start_published(
            StartPublishedRunRequestV2(RUN, looped.revision_hash, bindings)
        )
        assert isinstance(started, DurableRunCreated)
        runtime.launch()
        _wait_for_run(runtime, RUN, RunState.COMPLETED)

        assert isinstance(
            starter.fork_run(ForkRunRequest(RUN, "loop", "implement")),
            DurableRunForkLoopUnsupported,
        )
    finally:
        runtime.close()


def test_full_run_fork_starts_at_entry_and_fork_of_fork_flattens_reuse(
    tmp_path: Path,
) -> None:
    runtime, recording = _runtime(tmp_path)
    try:
        starter, _workflow = _completed_origin(runtime)
        full = starter.fork_run(ForkRunRequest(RUN, "full", "implement"))
        assert isinstance(full, DurableRunForkCreated)
        full_successor = full.run.run_id
        _wait_for_run(runtime, full_successor, RunState.COMPLETED)
        assert full.fork.reused_nodes == ()

        partial = starter.fork_run(ForkRunRequest(RUN, "partial", "review"))
        assert isinstance(partial, DurableRunForkCreated)
        _wait_for_run(runtime, partial.run.run_id, RunState.COMPLETED)

        nested = starter.fork_run(
            ForkRunRequest(partial.run.run_id, "nested-review", "review")
        )
        assert isinstance(nested, DurableRunForkCreated)
        _wait_for_run(runtime, nested.run.run_id, RunState.COMPLETED)
        assert tuple(entry.node_id for entry in nested.fork.reused_nodes) == (
            "implement",
        )
        assert nested.fork.reused_nodes[0].source_run_id == RUN
        assert recording.opened is not None
        assert [request.node_id for request in recording.opened.requests] == [
            "implement",
            "review",
            "implement",
            "review",
            "review",
            "review",
        ]
    finally:
        runtime.close()


@pytest.mark.parametrize("corruption", ["fork-hash", "reused-source"])
def test_run_projection_refuses_corrupt_fork_headers_and_reuse_evidence(
    tmp_path: Path, corruption: str
) -> None:
    runtime, _recording = _runtime(tmp_path)
    try:
        starter, _workflow = _completed_origin(runtime)
        created = starter.fork_run(ForkRunRequest(RUN, "corrupt", "review"))
        assert isinstance(created, DurableRunForkCreated)
        with runtime.engine.connect() as connection:
            connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
            if corruption == "fork-hash":
                connection.exec_driver_sql("DROP TRIGGER run_forks_no_update")
                connection.execute(run_forks.update().values(fork_hash="0" * 64))
                projected_run = RUN
            else:
                connection.exec_driver_sql("DROP TRIGGER node_receipts_v3_no_delete")
                connection.execute(
                    node_receipts_v3.delete().where(
                        node_receipts_v3.c.node_execution_id
                        == NodeExecutionId.for_node(
                            RUN, created.run.revision_hash, "implement"
                        ).value
                    )
                )
                projected_run = created.run.run_id
            connection.commit()
            connection.exec_driver_sql("PRAGMA foreign_keys=ON")

        response = durable_api_client(runtime).get(
            API_PREFIX + "/runs/" + encode_public_run_reference(projected_run)
        )
        assert response.status_code == 500
        assert response.json()["type"].endswith(":durable-state-corrupt")
    finally:
        runtime.close()


def test_run_projection_refuses_more_than_one_bounded_successor_lineage(
    tmp_path: Path,
) -> None:
    runtime, _recording = _runtime(tmp_path)
    try:
        starter, _workflow = _completed_origin(runtime)
        for index in range(MAXIMUM_RUN_FORK_SUCCESSORS + 1):
            assert isinstance(
                starter.fork_run(
                    ForkRunRequest(RUN, f"successor-{index}", "implement")
                ),
                DurableRunForkCreated,
            )

        assert durable_queries(runtime.engine).get_run(RUN) == ProjectionTooLarge()
    finally:
        runtime.close()


SILENT_SECOND_PUBLISHER = Publisher("fix", depends_on=("build",))
"""A second publisher naming no `starts_from`: a document no start admits today."""


RUN_OF_OTHER_RULES = RunId("v3/started-under-other-rules")
"""The finished run whose document this build would refuse to start today."""


def _frozen_configuration(
    runtime: DbosRuntime, revision: WorkflowRevision, binding_set_hash: str
) -> RunConfigurationRevision:
    """What a build whose rules admitted this document froze when it started it.

    Built from the document's own resolved references, so the seeded run stands
    on the configuration a real start writes rather than on a stand-in that
    would refuse the fork as corrupt before it is judged at all.
    """

    graph = parse_workflow_document(revision.document)
    assert isinstance(graph, WorkflowGraphV3), graph
    resolved = resolve_document_references(graph, DbosCatalogStore(runtime.engine))
    assert isinstance(resolved, ExecutableDocument), resolved
    return RunConfigurationRevision(
        WorkflowRevisionHash(revision.revision_hash.value),
        AgentBindingSetHash(binding_set_hash),
        resolved.resolutions,
    )


def _finished_run_of(runtime: DbosRuntime, revision: WorkflowRevision) -> None:
    """Seed a finished run bound to this document, as the build that ran it left it.

    A run's bindings are immutable and no door of this build starts a document
    with a silent second publisher, so the only run of one is a run an earlier
    build's rules admitted. It stands on the bindings and the terminal fact the
    origin of this module already carries, and on the configuration that build
    would have frozen for it.
    """

    with runtime.engine.connect() as connection:
        origin = (
            connection.execute(sa.select(runs).where(runs.c.run_id == RUN.value))
            .mappings()
            .one()
        )
        bindings = (
            connection.execute(
                sa.select(run_agent_bindings).where(
                    run_agent_bindings.c.run_id == RUN.value
                )
            )
            .mappings()
            .all()
        )
        configuration = _frozen_configuration(
            runtime, revision, str(origin["agent_binding_set_hash"])
        )
        connection.execute(
            run_configuration_revisions.insert().values(
                revision_hash=configuration.revision_hash.value,
                preimage=configuration.preimage,
            )
        )
        connection.execute(
            runs.insert().values(
                run_id=RUN_OF_OTHER_RULES.value,
                bootstrap_workflow_id=f"bootstrap-{RUN_OF_OTHER_RULES.value}",
                revision_hash=revision.revision_hash.value,
                workflow_format_version=origin["workflow_format_version"],
                run_configuration_revision_hash=configuration.revision_hash.value,
                agent_binding_set_hash=origin["agent_binding_set_hash"],
                current_node_id=SILENT_SECOND_PUBLISHER.node_id,
                current_round_ordinal=origin["current_round_ordinal"],
                state=RunState.COMPLETED.value,
                state_version=origin["state_version"],
                last_event_sequence=0,
                terminal_hash=origin["terminal_hash"],
            )
        )
        connection.execute(
            run_agent_bindings.insert(),
            [
                {
                    **dict(binding),
                    "run_id": RUN_OF_OTHER_RULES.value,
                    "revision_hash": revision.revision_hash.value,
                }
                for binding in bindings
            ],
        )
        connection.commit()


def test_fork_refuses_a_document_this_build_would_no_longer_start(
    tmp_path: Path,
) -> None:
    """A fork enqueues a successor of the origin's document, so it asks what a
    start asks: a document whose rules admitted it when the origin ran, and
    which this build refuses, is refused here rather than carried to a claim."""

    runtime, _recording = _runtime(tmp_path)
    try:
        starter, _workflow = _completed_origin(runtime)
        operation = push_operation()
        grant = push_grant(operation)
        publish_pinned_revisions(runtime.engine, operation, grant)
        unadmitted = WorkflowRevision(
            workflow_document("two-publishers", (BUILD, SILENT_SECOND_PUBLISHER), grant)
        )
        DbosWorkflowRevisionPublisher(runtime.engine).publish(unadmitted)
        _finished_run_of(runtime, unadmitted)

        refused = starter.fork_run(
            ForkRunRequest(RUN_OF_OTHER_RULES, "other-rules", BUILD.node_id)
        )

        assert isinstance(refused, DurableRunForkDocumentNotExecutable)
        assert SILENT_SECOND_PUBLISHER.node_id in refused.reason
        assert "starts_from" in refused.reason
        successor = successor_run_id_for(
            RunForkCommandId.for_request(RUN_OF_OTHER_RULES, "other-rules")
        )
        with runtime.engine.connect() as connection:
            assert (
                connection.scalar(
                    sa.select(sa.func.count())
                    .select_from(runs)
                    .where(runs.c.run_id == successor.value)
                )
                == 0
            )
            assert (
                connection.scalar(sa.select(sa.func.count()).select_from(run_forks))
                == 0
            )
            assert (
                connection.scalar(
                    sa.select(sa.func.count())
                    .select_from(effect_intents)
                    .where(
                        effect_intents.c.operation_name
                        == AdapterOperationName.CLAIM_WORK_ITEM.value
                    )
                )
                == 0
            )

        assert isinstance(
            starter.fork_run(ForkRunRequest(RUN, "admitted", "review")),
            DurableRunForkCreated,
        )
    finally:
        runtime.close()


def test_fork_refuses_an_origin_configuration_that_is_no_longer_a_frame(
    tmp_path: Path,
) -> None:
    """The fork store reads the shared frame layout but keeps its own name for
    what a broken frame means here: the durable state is corrupt, not a
    reader's exception surfacing at the port."""

    runtime, _recording = _runtime(tmp_path)
    try:
        starter, _workflow = _completed_origin(runtime)
        with runtime.engine.connect() as connection:
            connection.exec_driver_sql(
                "DROP TRIGGER run_configuration_revisions_no_update"
            )
            connection.execute(
                run_configuration_revisions.update().values(preimage=b"not-a-frame")
            )
            connection.commit()

        assert isinstance(
            starter.fork_run(ForkRunRequest(RUN, "unframable", "review")),
            DurableRunForkStateCorrupt,
        )
        with runtime.engine.connect() as connection:
            assert (
                connection.scalar(sa.select(sa.func.count()).select_from(run_forks))
                == 0
            )
    finally:
        runtime.close()
