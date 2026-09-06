"""A V3 agent document of the one admitted shape becomes a started run.

**What this file claims, and where the rest lives.** These tests end at the
durable shape a start writes: the document is admitted, the run carries its own
V3 truth, and its agent node binds onto the attempt path with the exact role and
configuration it was started with. They deliberately never launch the runtime, so
no provider runs and nothing here reaches a terminal state.

That is a boundary of scope now rather than of capability. While this file was
written it was both: the attempt path was V2 to the bone and no runtime drove a
V3 line, so these starts went through an internal foundation seam that enqueued
nothing. H1b, H1c and H2 built the driver, the seam is gone, and every start here
is the public one. What happens after it is `test_v3_self_driving_run`.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from pathlib import Path
from typing import cast

import pytest
import sqlalchemy as sa

from atelier2.adapters.dbos.agent_catalog import DbosAgentConfigurationCatalog
from atelier2.adapters.dbos.catalog_store import DbosCatalogStore
from atelier2.adapters.dbos.node_binding_codec import EncodedAgentBindingV2
from atelier2.adapters.dbos.run_store import run_from_record_with_bindings
from atelier2.adapters.dbos.runtime import DbosRuntime
from atelier2.adapters.dbos.schema import (
    agent_attempts,
    run_configuration_revisions,
    run_events,
    runs,
)
from atelier2.adapters.dbos.starter import (
    DbosDurableRunStarter,
    DbosWorkflowRevisionPublisher,
)
from atelier2.adapters.dbos.workflow import _node_binding
from atelier2.adapters.project_verification import declared_project
from atelier2.api.openapi import API_PREFIX
from atelier2.contracts.agents import (
    AgentBinding,
    AgentBindingSet,
    AgentConfigurationRevision,
    AgentConfigurationRevisionFormatVersion,
    AgentExecutionCapability,
    AgentExecutorRevision,
    AgentRole,
    AuthMode,
    AuthProfileRevision,
    ProviderId,
)
from atelier2.contracts.budgets_v3 import BudgetField
from atelier2.contracts.executions import AgentExecutionRefusal
from atelier2.contracts.revisions_v3 import PublishedRevision, RevisionKind
from atelier2.contracts.run_bindings import RunBindingConflict, RunV3
from atelier2.contracts.run_configuration_v3 import (
    ReferenceSite,
    ResolvedReference,
    RunConfigurationRevision,
)
from atelier2.contracts.runs import (
    RunId,
    RunState,
    WorkflowRevision,
    WorkflowRevisionHash,
)
from atelier2.contracts.when import RecordedAt
from atelier2.contracts.workflows_v3 import VersionedReference
from atelier2.ports.agent_configurations import (
    AgentConfigurationRevisionCreated,
    AuthProfileRevisionCreated,
)
from atelier2.ports.agent_executions import AgentExecutorRegistration
from atelier2.ports.durable_runs import (
    DurableRunCreated,
    DurableRunExisting,
    StartPublishedRunRequestV2,
)
from atelier2.ports.published_revisions import (
    PublishedRevisionCreated,
    PublishedRevisionExisting,
)
from atelier2.ports.run_queries import RunFound
from tests.scenarios.agents import (
    agent_scratch_root,
    failing_agent_executor_factory,
    publish_checked_model_registry,
)
from tests.scenarios.api import durable_api_client, durable_queries
from tests.scenarios.durable_state import (
    canonical_loopback_effects,
    canonical_runtime_settings,
)
from tests.scenarios.projects import git_project
from tests.scenarios.workflows import ANY_JSON_SCHEMA, declared_output

ONE_AGENT_DOCUMENT = b"""format_version: 3
name: One agent
nodes:
  - id: implement
    type: agent
    role: builder
    mode: headless
    instruction: Do the one thing this chain is for.
""" + declared_output()

TURN_BOUNDED_BUDGET = json.dumps(
    {
        BudgetField.ATTEMPT_DEADLINE_SECONDS.value: 900,
        BudgetField.MAXIMUM_ASSISTANT_TURNS.value: 8,
    }
).encode()
DEADLINE_ONLY_BUDGET = json.dumps(
    {BudgetField.ATTEMPT_DEADLINE_SECONDS.value: 900}
).encode()

EXECUTABLE_V2_DOCUMENT = b"""format_version: 2
start: implement
nodes:
  - {id: done, type: subworkflow, operation: add, operands: [2, 3], next: null}
  - {id: implement, type: agent, role: builder, job: build, next: done}
"""

RUN = RunId("v3/one-agent")


@pytest.fixture
def runtime(tmp_path: Path) -> Iterator[DbosRuntime]:
    started = DbosRuntime(
        canonical_runtime_settings(
            tmp_path, "v3-start-test", agent_scratch_root(tmp_path)
        ),
        canonical_loopback_effects(tmp_path),
        (failing_agent_executor_factory("exact", []),),
    )
    started.initialize_storage()
    try:
        yield started
    finally:
        started.close()


def publish(
    runtime: DbosRuntime,
    document: bytes = ONE_AGENT_DOCUMENT,
    roles: tuple[str, ...] = ("builder",),
) -> tuple[WorkflowRevision, AgentBindingSet]:
    """Publish one V3 document, the schema it pins, and every role it declares."""
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
    workflow = WorkflowRevision(document)
    DbosWorkflowRevisionPublisher(runtime.engine).publish(workflow)
    return workflow, AgentBindingSet(
        tuple(
            AgentBinding(AgentRole(role), configuration.revision_hash) for role in roles
        )
    )


def publish_budgeted(
    runtime: DbosRuntime, budget_document: bytes
) -> tuple[WorkflowRevision, AgentBindingSet]:
    """Publish a budget revision and a one-agent document that pins it."""
    budget = PublishedRevision(RevisionKind.BUDGET_POLICY, budget_document)
    published = DbosCatalogStore(runtime.engine).publish_revision(budget)
    assert isinstance(
        published, (PublishedRevisionCreated, PublishedRevisionExisting)
    ), published
    document = (
        b"""format_version: 3
name: One agent
nodes:
  - id: implement
    type: agent
    role: builder
    mode: headless
    instruction: Do the one thing this chain is for.
    budget: {ref: build-budget, revision: %s}
"""
        % budget.revision_hash.value.encode("ascii")
        + declared_output()
    )
    return publish(runtime, document)


@pytest.mark.proves("a-v3-agent-document-starts-and-binds-its-node")
def test_a_v3_agent_document_of_the_admitted_shape_starts(
    runtime: DbosRuntime,
) -> None:
    """The run this document becomes: its own format, its own entry node.

    This asserts the durable shape a start writes, and stops there: the runtime
    is never launched, so the run stands at STARTED with nothing terminal about
    it. That it then moves under a launched runtime is the driver sentence's
    claim, proven where the line actually runs.
    """
    workflow, bindings = publish(runtime)

    result = DbosDurableRunStarter(
        runtime.engine,
        runtime.settings,
        runtime.agent_executor_registry,
    ).start_published(StartPublishedRunRequestV2(RUN, workflow.revision_hash, bindings))

    assert isinstance(result, DurableRunCreated)
    with runtime.engine.connect() as connection:
        record = (
            connection.execute(sa.select(runs).where(runs.c.run_id == RUN.value))
            .mappings()
            .one()
        )
    assert int(record["workflow_format_version"]) == 3
    assert str(record["current_node_id"]) == "implement"
    assert RunState(str(record["state"])) is RunState.STARTED
    assert record["terminal_hash"] is None


@pytest.mark.proves("a-v3-agent-document-starts-and-binds-its-node")
def test_a_started_v3_run_reads_back_as_its_own_shape(runtime: DbosRuntime) -> None:
    """Not a V2 run wearing a new number: a V3 row is a V3 run."""
    workflow, bindings = publish(runtime)
    DbosDurableRunStarter(
        runtime.engine,
        runtime.settings,
        runtime.agent_executor_registry,
    ).start_published(StartPublishedRunRequestV2(RUN, workflow.revision_hash, bindings))

    with runtime.engine.connect() as connection:
        record = (
            connection.execute(sa.select(runs).where(runs.c.run_id == RUN.value))
            .mappings()
            .one()
        )
        run = run_from_record_with_bindings(connection, record)

    assert isinstance(run, RunV3)
    assert [binding.role.value for binding in run.agent_bindings] == ["builder"]
    assert run.binding_set_hash == bindings.binding_set_hash


@pytest.mark.proves("a-bound-unstarted-run-refuses-when-its-executor-is-unavailable")
def test_a_bound_v3_run_refuses_before_an_attempt_when_its_executor_is_unavailable(
    runtime: DbosRuntime,
    tmp_path: Path,
) -> None:
    """A restart keeps a declared V3 binding but honestly declines to launch it."""
    workflow, bindings = publish(runtime)
    started = DbosDurableRunStarter(
        runtime.engine,
        runtime.settings,
        runtime.agent_executor_registry,
    ).start_published(StartPublishedRunRequestV2(RUN, workflow.revision_hash, bindings))
    assert isinstance(started, DurableRunCreated)
    settings = runtime.settings
    runtime.close()

    unavailable_factory = failing_agent_executor_factory("exact", [])
    restarted = DbosRuntime(
        settings,
        canonical_loopback_effects(tmp_path),
        (AgentExecutorRegistration.unavailable(unavailable_factory),),
    )
    try:
        restarted.launch()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            found = durable_queries(restarted.engine).get_run(RUN)
            if (
                isinstance(found, RunFound)
                and found.projection.run.state is RunState.FAILED
            ):
                break
            time.sleep(0.025)
        else:
            raise AssertionError("V3 run did not refuse its unavailable executor")

        assert unavailable_factory.opens == 0
        with restarted.engine.connect() as connection:
            events = tuple(
                connection.execute(
                    sa.select(run_events)
                    .where(run_events.c.run_id == RUN.value)
                    .order_by(run_events.c.event_sequence)
                ).mappings()
            )
            assert (
                connection.scalar(
                    sa.select(sa.func.count()).select_from(agent_attempts)
                )
                == 0
            )
        assert len(events) == 1
        assert events[0]["event_kind"] == "AGENT_FAILED"
        assert events[0]["payload"] == (
            AgentExecutionRefusal.EXECUTOR_BINDING_UNAVAILABLE.value.encode("ascii")
        )
        assert events[0]["agent_attempt_id"] is None
        assert events[0]["attempt_ordinal"] is None
    finally:
        restarted.close()


@pytest.mark.proves("a-node-binding-is-decided-where-no-store-can-be-reached")
def test_the_durable_step_refuses_a_node_its_run_does_not_stand_on(
    runtime: DbosRuntime,
) -> None:
    """The refusal a caller of the real adapter path sees, named and typed.

    The run stands on `implement`, so a node workflow that woke for `review` --
    the routine shape of a re-driven workflow whose run has moved on -- owns
    nothing to bind. It is a `RunBindingConflict` rather than the store's
    `RunTransitionConflict`, the same class its sibling precondition in
    `bootstrap_run_binding` has always raised: neither writes a transition, both
    refuse a durable binding that does not hold.
    """
    workflow, bindings = publish(runtime)
    DbosDurableRunStarter(
        runtime.engine,
        runtime.settings,
        runtime.agent_executor_registry,
    ).start_published(StartPublishedRunRequestV2(RUN, workflow.revision_hash, bindings))

    with pytest.raises(RunBindingConflict, match="does not own current STARTED node"):
        _node_binding(runtime.datasource, RUN, workflow.revision_hash, "review", None)


@pytest.mark.proves("a-v3-agent-document-starts-and-binds-its-node")
def test_the_v3_agent_node_binds_with_the_exact_role_and_configuration(
    runtime: DbosRuntime,
) -> None:
    """The binding replays what the run was started with, field for field."""
    workflow, bindings = publish(runtime)
    DbosDurableRunStarter(
        runtime.engine,
        runtime.settings,
        runtime.agent_executor_registry,
    ).start_published(StartPublishedRunRequestV2(RUN, workflow.revision_hash, bindings))

    encoded = _node_binding(
        runtime.datasource, RUN, workflow.revision_hash, "implement", None
    )
    binding = cast(EncodedAgentBindingV2, encoded)

    assert binding["type"] == "agent-v2"
    assert binding["role"] == "builder"
    assert binding["job"] == "Do the one thing this chain is for."
    assert binding["model"] == "opus"
    assert binding["executor_revision"] == "exact/v1"
    assert (
        binding.get("requested_capability") == AgentExecutionCapability.HEADLESS.value
    )
    assert (
        binding["configuration_hash"]
        == bindings.bindings[0].agent_configuration_revision_hash.value
    )
    assert binding.get("output_schema_document") == ANY_JSON_SCHEMA.document.decode(
        "utf-8"
    )
    assert "project_commit" not in binding
    assert "maximum_assistant_turns" not in binding


@pytest.mark.parametrize(
    ("budget_document", "expected_turns"),
    (
        (TURN_BOUNDED_BUDGET, 8),
        (DEADLINE_ONLY_BUDGET, None),
    ),
    ids=["pinned turn bound", "budget without a turn bound"],
)
@pytest.mark.proves("a-pinned-budget-turn-bound-is-the-tool-attempt-ceiling")
def test_the_binding_carries_the_turn_bound_the_published_budget_named(
    runtime: DbosRuntime,
    budget_document: bytes,
    expected_turns: int | None,
) -> None:
    """The 8 is the published revision's, not a number the test injected."""
    workflow, bindings = publish_budgeted(runtime, budget_document)
    started = DbosDurableRunStarter(
        runtime.engine,
        runtime.settings,
        runtime.agent_executor_registry,
    ).start_published(StartPublishedRunRequestV2(RUN, workflow.revision_hash, bindings))
    assert isinstance(started, DurableRunCreated)

    binding = cast(
        EncodedAgentBindingV2,
        _node_binding(
            runtime.datasource, RUN, workflow.revision_hash, "implement", None
        ),
    )

    if expected_turns is None:
        assert "maximum_assistant_turns" not in binding
    else:
        assert binding.get("maximum_assistant_turns") == expected_turns


@pytest.mark.proves("an-attempt-works-in-the-tree-its-own-binding-pinned")
def test_the_binding_of_a_node_pins_the_project_source_it_will_work_in(
    runtime: DbosRuntime, tmp_path: Path
) -> None:
    """Pinned where the binding is composed, so a later commit changes nothing."""
    workflow, bindings = publish(runtime)
    DbosDurableRunStarter(
        runtime.engine,
        runtime.settings,
        runtime.agent_executor_registry,
    ).start_published(StartPublishedRunRequestV2(RUN, workflow.revision_hash, bindings))
    root = tmp_path / "project"
    pinned = git_project(root, {"pyproject.toml": "[project]\nname = 'pinned'\n"})

    binding = cast(
        EncodedAgentBindingV2,
        _node_binding(
            runtime.datasource,
            RUN,
            workflow.revision_hash,
            "implement",
            declared_project(root, runtime.settings.database_path),
        ),
    )

    assert (binding.get("project_commit"), binding.get("project_tree")) == (
        pinned.commit,
        pinned.tree,
    )


@pytest.mark.proves("a-v3-run-answers-over-http-with-its-own-resource")
@pytest.mark.proves("a-run-carries-when-it-started-and-ended")
@pytest.mark.proves("the-run-list-carries-when")
def test_the_public_start_route_answers_a_v3_revision_with_its_run_resource(
    runtime: DbosRuntime,
) -> None:
    """The last door: a V3 revision started over HTTP answers with its run.

    Until this head the route could not answer at all. First it wrote the run and
    then raised on the missing resource, handing the caller a 500 over durable
    state; then a named stopgap refused the start outright so at least nothing was
    left behind. Both are gone now, because the answer exists: the run is created
    and rendered in its own format, not dressed up as a V2 one.
    """
    workflow, bindings = publish(runtime)

    started = durable_api_client(runtime).post(
        API_PREFIX + "/runs",
        json={
            "workflow_format_version": 2,
            "run_id": RUN.value,
            "workflow_revision_hash": workflow.revision_hash.value,
            "agent_bindings": [
                {
                    "role": binding.role.value,
                    "agent_configuration_revision_hash": (
                        binding.agent_configuration_revision_hash.value
                    ),
                }
                for binding in bindings.bindings
            ],
        },
    )

    assert started.status_code == 201
    answered = started.json()
    assert answered["workflow_format_version"] == 3
    assert answered["run_id"] == RUN.value
    assert answered["state"] == RunState.STARTED.value
    assert answered["workflow_revision_hash"] == workflow.revision_hash.value
    assert answered["terminal_hash"] is None
    assert answered["ended_at"] is None
    RecordedAt(answered["started_at"])
    listed = durable_api_client(runtime).get(API_PREFIX + "/runs")
    assert listed.status_code == 200
    listed_run = next(
        item["run"]
        for item in listed.json()["items"]
        if item["run"]["run_id"] == RUN.value
    )
    assert listed_run["started_at"] == answered["started_at"]
    assert listed_run["ended_at"] is None
    assert [entry["node_id"] for entry in answered["node_rail"]] == ["implement"]
    with runtime.engine.connect() as connection:
        assert connection.scalar(sa.select(sa.func.count()).select_from(runs)) == 1


@pytest.mark.proves("a-v3-agent-document-starts-and-binds-its-node")
def test_a_started_v3_run_reads_back_through_the_durable_query(
    runtime: DbosRuntime,
) -> None:
    """Reading a V3 run must answer it, not crash on a column nobody selected.

    The run projection is what every read of a run is built from, and its column
    list was written when no V3 run could reach one: it omitted the configuration
    revision a `RunV3` is bound to, so projecting one raised `NoSuchColumnError`
    from inside the adapter. Nothing noticed, because the public start refused the
    family before a V3 run could ever be read.

    The API above this still cannot render a V3 run, and the route refuses a start
    it could not answer rather than dressing one up as a V2 shape. That refusal is
    the point: this layer must reach it instead of crashing under it.
    """
    workflow, bindings = publish(runtime)
    DbosDurableRunStarter(
        runtime.engine,
        runtime.settings,
        runtime.agent_executor_registry,
    ).start_published(StartPublishedRunRequestV2(RUN, workflow.revision_hash, bindings))

    found = durable_queries(runtime.engine).get_run(RUN)

    assert isinstance(found, RunFound)
    run = found.projection.run
    assert isinstance(run, RunV3)
    assert run.run_id == RUN
    assert run.current_node_id == "implement"


@pytest.mark.proves("a-v3-agent-document-starts-and-binds-its-node")
def test_the_first_start_and_its_retry_answer_the_same_run_shape(
    runtime: DbosRuntime,
) -> None:
    """A first start used to answer RunV2 while its retry answered RunV3."""
    workflow, bindings = publish(runtime)
    starter = DbosDurableRunStarter(
        runtime.engine,
        runtime.settings,
        runtime.agent_executor_registry,
    )
    request = StartPublishedRunRequestV2(RUN, workflow.revision_hash, bindings)

    created = starter.start_published(request)
    existing = starter.start_published(request)

    assert isinstance(created, DurableRunCreated)
    assert isinstance(existing, DurableRunExisting)
    assert isinstance(created.run, RunV3)
    assert isinstance(existing.run, RunV3)
    assert created.run == existing.run


@pytest.mark.proves(
    "a-supervised-v3-run-records-the-configuration-it-was-started-under"
)
def test_a_v3_start_binds_the_exact_run_configuration(
    runtime: DbosRuntime,
) -> None:
    """A V3 run binds the configuration it was started under, or it is not one.

    This was named after the foundation start and said "both V3 starts", from
    when a private second door persisted V3 runs that nothing executed. #216
    deleted that door; there is one start, and the claim is about it.

    The admitted shape declares exactly one versioned reference -- the schema its
    one output pins -- so its resolution matrix is that reference and nothing
    else, exact rather than guessed. A run persisted without it would be a V3 run
    whose own snapshot nobody could name.
    """
    workflow, bindings = publish(runtime)
    DbosDurableRunStarter(
        runtime.engine,
        runtime.settings,
        runtime.agent_executor_registry,
    ).start_published(StartPublishedRunRequestV2(RUN, workflow.revision_hash, bindings))
    expected = RunConfigurationRevision(
        WorkflowRevisionHash(workflow.revision_hash.value),
        bindings.binding_set_hash,
        (
            ResolvedReference(
                ReferenceSite("outputs.schema", "implement", "result"),
                RevisionKind.SCHEMA,
                VersionedReference(
                    ref="result-schema", revision=ANY_JSON_SCHEMA.revision_hash.value
                ),
                ANY_JSON_SCHEMA.revision_hash,
            ),
        ),
    )

    with runtime.engine.connect() as connection:
        record = (
            connection.execute(sa.select(runs).where(runs.c.run_id == RUN.value))
            .mappings()
            .one()
        )
        stored = connection.scalar(
            sa.select(run_configuration_revisions.c.preimage).where(
                run_configuration_revisions.c.revision_hash
                == expected.revision_hash.value
            )
        )

    assert record["run_configuration_revision_hash"] == expected.revision_hash.value
    assert stored is not None
    assert bytes(stored) == expected.preimage
