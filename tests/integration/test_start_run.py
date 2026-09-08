from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from itertools import repeat
from pathlib import Path
from threading import Barrier
from typing import Any, NoReturn

import pytest
import sqlalchemy as sa
from dbos import DBOSClient, EnqueueOptions
from sqlalchemy import exc
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session

from atelier2.adapters.dbos.catalog_store import DbosCatalogStore
from atelier2.adapters.dbos.runtime import (
    DbosRuntime,
    DbosRuntimeSettings,
    create_canonical_engine,
)
from atelier2.adapters.dbos.schema import (
    PRODUCT_TABLE_NAMES,
    SCHEMA_VERSION,
    MigrationRequired,
    UnsupportedSchemaVersion,
    initialize_schema,
    workflow_revisions,
)
from atelier2.adapters.dbos.starter import (
    DbosDurableRunStarter,
    DbosWorkflowRevisionPublisher,
)
from atelier2.adapters.dbos.work_item_intents import issue_work_item_order
from atelier2.adapters.dbos.workflow import bootstrap_run_binding
from atelier2.adapters.dbos.workflow_ids import bootstrap_workflow_id_for
from atelier2.adapters.loopback import LoopbackEffectAdapterFactory
from atelier2.adapters.yaml_workflows import parse_workflow_document
from atelier2.application.publish_workflow_revision import (
    DurableStateCorrupt,
    PublicationInvalid,
    publish_workflow_revision,
)
from atelier2.application.start_published_run import (
    AuthoredOrder,
    RunCreated,
    RunIdentityConflict,
    RunInputRefused,
    UncastAgentRoles,
    start_published_run,
)
from atelier2.contracts.agents import AgentBindingSet
from atelier2.contracts.effects import AdapterRevision, EffectDestination
from atelier2.contracts.orders import ObservedWorkItemOrderValue
from atelier2.contracts.queue_projection import TrackerItemReference
from atelier2.contracts.revisions_v3 import PublishedRevision, RevisionKind
from atelier2.contracts.run_bindings import AnyRun
from atelier2.contracts.runs import RunId, RunState, WorkflowRevision
from atelier2.contracts.when import RecordedAt
from atelier2.contracts.work_items import (
    WORK_ITEM_ORDER_SCHEMA_DOCUMENT,
    WORK_ITEM_ORDER_SCHEMA_REVISION,
    ObservedWorkItemRevision,
    WorkItemChangeMarker,
    WorkItemKind,
    WorkItemScope,
)
from atelier2.ports.durable_runs import (
    DurableRunFormatNotExecutable,
    StartPublishedRunRequestV2,
    V3InputRefusal,
)
from tests.scenarios.agents import agent_scratch_root
from tests.scenarios.api import permissive_projection_limit
from tests.scenarios.run_waiting import wait_for_run_state
from tests.scenarios.runs import (
    publish_pinned_revisions,
    publish_revision,
    publish_v3_agent_bindings,
    start_published_v3_run,
)
from tests.scenarios.runtime import recording_exact_runtime
from tests.scenarios.workflows import (
    ANY_JSON_SCHEMA,
    OPEN_PR_OPERATION,
    V3_EFFECT_LINE_ACTION_NODE_ID,
    V3_EFFECT_LINE_AGENT_NODE_ID,
    V3_EFFECT_LINE_DOCUMENT,
    V3_EFFECT_LINE_WAIT_NODE_ID,
    graph_input_wait_line,
)

WORKFLOW_DOCUMENT = V3_EFFECT_LINE_DOCUMENT
PROVIDER_OUTPUT = b'"draft-17"'

V3_SUBWORKFLOW_DOCUMENT = b"""format_version: 3
name: An authored child is not executable yet
nodes:
  - id: child
    type: subworkflow
    workflow: {ref: child-workflow, revision: "0000000000000000000000000000000000000000000000000000000000000000"}
"""


@pytest.fixture
def storage(tmp_path: Path) -> Iterator[tuple[DbosRuntime, DbosDurableRunStarter]]:
    runtime = recording_exact_runtime(
        DbosRuntimeSettings(
            tmp_path / "atelier.sqlite",
            "executor-A",
            agent_scratch_root=agent_scratch_root(tmp_path),
        ),
        LoopbackEffectAdapterFactory(
            tmp_path / "external.sqlite",
            AdapterRevision("loopback-v1"),
            EffectDestination("loopback-test"),
        ),
        PROVIDER_OUTPUT,
    )
    runtime.initialize_storage()
    try:
        yield (
            runtime,
            DbosDurableRunStarter(
                runtime.engine,
                runtime.settings,
                runtime.agent_executor_registry,
            ),
        )
    finally:
        runtime.close()


def revision(document: bytes = WORKFLOW_DOCUMENT) -> WorkflowRevision:
    return WorkflowRevision(document)


def start(
    runtime: DbosRuntime,
    starter: DbosDurableRunStarter,
    run_id: str = "run-1",
    document: bytes = WORKFLOW_DOCUMENT,
) -> AnyRun:
    publish_pinned_revisions(runtime.engine, ANY_JSON_SCHEMA, OPEN_PR_OPERATION)
    return start_published_v3_run(
        runtime.engine,
        runtime.settings,
        RunId(run_id),
        revision(document),
        runtime.agent_executor_registry,
    )


def start_result(
    runtime: DbosRuntime,
    starter: DbosDurableRunStarter,
    run_id: str = "run-1",
    document: bytes = WORKFLOW_DOCUMENT,
):
    publish_pinned_revisions(runtime.engine, ANY_JSON_SCHEMA, OPEN_PR_OPERATION)
    bindings = publish_v3_agent_bindings(
        runtime.engine, runtime.agent_executor_registry
    )
    publish_revision(runtime.engine, revision(document))
    return start_published_run(
        RunId(run_id), revision(document).revision_hash, bindings, starter
    )


def count(engine: sa.Engine, table: str) -> int:
    with engine.connect() as connection:
        return int(connection.scalar(sa.text(f"SELECT COUNT(*) FROM {table}")) or 0)


def test_start_commits_the_run_and_its_enqueue_atomically(
    storage: tuple[DbosRuntime, DbosDurableRunStarter],
) -> None:
    runtime, starter = storage

    started = start(runtime, starter)

    assert started.state is RunState.STARTED
    with runtime.engine.connect() as connection:
        assert connection.execute(
            sa.text(
                "SELECT run_id, revision_hash, current_node_id, state, "
                "state_version, last_event_sequence, terminal_hash FROM runs"
            )
        ).all() == [
            (
                "run-1",
                revision().revision_hash.value,
                V3_EFFECT_LINE_AGENT_NODE_ID,
                "STARTED",
                0,
                0,
                None,
            )
        ]
        assert connection.execute(
            sa.text("SELECT workflow_uuid, application_version FROM workflow_status")
        ).all() == [(bootstrap_workflow_id_for(RunId("run-1")), "executor-A")]


def test_a_started_run_s_work_item_order_reads_back_its_declared_scope(
    storage: tuple[DbosRuntime, DbosDurableRunStarter],
) -> None:
    """`issue_work_item_order` (`advancer.py`'s one door onto a bound work item)
    answers from the same starter and schema pin `workflows/issue-to-pr.yaml`
    declares, so this proves the scope a real start persisted reads back
    through it -- not through a fake port or a hand-called document reader.
    """

    runtime, starter = storage
    document = graph_input_wait_line(WORK_ITEM_ORDER_SCHEMA_REVISION.value)
    publish_pinned_revisions(
        runtime.engine,
        ANY_JSON_SCHEMA,
        PublishedRevision(RevisionKind.SCHEMA, WORK_ITEM_ORDER_SCHEMA_DOCUMENT),
    )
    publish_revision(runtime.engine, revision(document))
    observed = ObservedWorkItemRevision(
        TrackerItemReference("gh:9001"),
        WorkItemKind.ISSUE,
        b"## Bereich\nsrc/atelier2/contracts/work_items.py\n",
        WorkItemChangeMarker('W/"scope-1"'),
        RecordedAt("2026-09-06T09:00:00Z"),
    )

    result = start_published_run(
        RunId("run-with-scope"),
        revision(document).revision_hash,
        (),
        starter,
        orders=(AuthoredOrder("context", ObservedWorkItemOrderValue(observed)),),
    )

    assert isinstance(result, RunCreated)
    with Session(runtime.engine) as session:
        order = issue_work_item_order(session, result.run.run_id)
    assert order.scope == WorkItemScope(("src/atelier2/contracts/work_items.py",))


def test_a_malformed_declared_scope_refuses_the_start_by_name_not_as_corruption(
    storage: tuple[DbosRuntime, DbosDurableRunStarter],
) -> None:
    """A tracker item's own bad `## Bereich` scope-list line is the author's
    mistake, not a lie the store told: it must refuse the order by name, never
    surface as the generic `DurableStateCorrupt` a real storage defect would.
    """

    runtime, starter = storage
    document = graph_input_wait_line(WORK_ITEM_ORDER_SCHEMA_REVISION.value)
    publish_pinned_revisions(
        runtime.engine,
        ANY_JSON_SCHEMA,
        PublishedRevision(RevisionKind.SCHEMA, WORK_ITEM_ORDER_SCHEMA_DOCUMENT),
    )
    publish_revision(runtime.engine, revision(document))
    observed = ObservedWorkItemRevision(
        TrackerItemReference("gh:9002"),
        WorkItemKind.ISSUE,
        b"## Bereich\n../etc/passwd\n",
        WorkItemChangeMarker('W/"scope-2"'),
        RecordedAt("2026-09-06T09:00:00Z"),
    )

    result = start_published_run(
        RunId("run-with-malformed-scope"),
        revision(document).revision_hash,
        (),
        starter,
        orders=(AuthoredOrder("context", ObservedWorkItemOrderValue(observed)),),
    )

    assert isinstance(result, RunInputRefused)
    assert result.name == "context"
    assert result.refusal is V3InputRefusal.VALUE_REFUSED
    assert result.detail is not None
    assert "../etc/passwd" in result.detail


def test_an_empty_declared_scope_refuses_the_start_by_name_not_as_corruption(
    storage: tuple[DbosRuntime, DbosDurableRunStarter],
) -> None:
    runtime, starter = storage
    document = graph_input_wait_line(WORK_ITEM_ORDER_SCHEMA_REVISION.value)
    publish_pinned_revisions(
        runtime.engine,
        ANY_JSON_SCHEMA,
        PublishedRevision(RevisionKind.SCHEMA, WORK_ITEM_ORDER_SCHEMA_DOCUMENT),
    )
    publish_revision(runtime.engine, revision(document))
    observed = ObservedWorkItemRevision(
        TrackerItemReference("gh:9003"),
        WorkItemKind.ISSUE,
        b"no scope list in this body",
        WorkItemChangeMarker('W/"scope-3"'),
        RecordedAt("2026-09-06T09:00:00Z"),
    )

    result = start_published_run(
        RunId("run-with-empty-scope"),
        revision(document).revision_hash,
        (),
        starter,
        orders=(AuthoredOrder("context", ObservedWorkItemOrderValue(observed)),),
    )

    assert isinstance(result, RunInputRefused)
    assert result.name == "context"
    assert result.refusal is V3InputRefusal.VALUE_REFUSED
    assert result.detail is not None
    assert "no scope list" in result.detail


def test_raise_after_real_enqueue_rolls_back_the_run_and_its_workflow(
    storage: tuple[DbosRuntime, DbosDurableRunStarter], monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, starter = storage
    real_enqueue = DBOSClient.enqueue_in_transaction

    def enqueue_then_raise(
        self: DBOSClient,
        connection: Connection | Session,
        options: EnqueueOptions,
        *args: Any,
        **kwargs: Any,
    ) -> NoReturn:
        real_enqueue(self, connection, options, *args, **kwargs)
        raise RuntimeError("injected after real enqueue")

    monkeypatch.setattr(DBOSClient, "enqueue_in_transaction", enqueue_then_raise)

    assert isinstance(start_result(runtime, starter), DurableStateCorrupt)

    assert count(runtime.engine, "workflow_revisions") == 1
    assert count(runtime.engine, "runs") == 0
    assert count(runtime.engine, "workflow_status") == 0


@pytest.mark.proves("no-write-path-can-be-reached-with-a-dependency-left-out")
def test_a_starter_cannot_be_built_without_the_registry_it_binds_against(
    storage: tuple[DbosRuntime, DbosDurableRunStarter],
) -> None:
    """There is no starter with an empty registry to construct by accident.

    Leaving the registry out used to build one that attested nothing, so a bound
    executor was missing rather than refused. It is now what a starter is made
    of, and omitting it is not a permissive start — it is not a starter.
    """
    runtime, _starter = storage

    with pytest.raises(TypeError):
        DbosDurableRunStarter(runtime.engine, runtime.settings)  # type: ignore[call-arg]


def test_invalid_graph_writes_no_revision_run_event_answer_or_dbos_workflow(
    storage: tuple[DbosRuntime, DbosDurableRunStarter],
) -> None:
    runtime, _starter = storage
    invalid = WORKFLOW_DOCUMENT.replace(
        b"depends_on: [implement]", b"depends_on: [missing]"
    )

    result = publish_workflow_revision(
        invalid,
        DbosWorkflowRevisionPublisher(runtime.engine),
        parse_workflow_document,
        permissive_projection_limit(),
        DbosCatalogStore(runtime.engine),
    )

    assert isinstance(result, PublicationInvalid)

    for table in PRODUCT_TABLE_NAMES - {"atelier_schema_versions"}:
        assert count(runtime.engine, table) == 0
    assert count(runtime.engine, "workflow_status") == 0


def test_an_uncast_v3_role_refuses_a_start_without_a_durable_run(
    storage: tuple[DbosRuntime, DbosDurableRunStarter],
) -> None:
    """A declared role no project default resolves refuses cleanly, before any write.

    This test once proved the same invariant -- an unbound start writes nothing
    durable -- through a V2 graph refused as `InvalidAgentBindings`. The V1/V2
    document grammar that made that refusal reachable is deleted (#901 slice 5,
    #934); a V3 document proves the surviving invariant through the refusal a
    V3 role actually gets when nothing casts it, `UncastAgentRoles`, since V3
    resolves each role against project defaults rather than accepting or
    refusing a whole binding set at once.
    """
    runtime, starter = storage
    publish_pinned_revisions(runtime.engine, ANY_JSON_SCHEMA, OPEN_PR_OPERATION)
    publish_revision(runtime.engine, revision())
    result = start_published_run(
        RunId("run-1"), revision().revision_hash, None, starter
    )

    assert isinstance(result, UncastAgentRoles)
    for table in PRODUCT_TABLE_NAMES - {
        "atelier_schema_versions",
        "workflow_revisions",
        "published_revisions",
    }:
        assert count(runtime.engine, table) == 0
    assert count(runtime.engine, "workflow_status") == 0


@pytest.mark.proves(
    "an-authored-v3-subworkflow-remains-unexecutable-without-start-writes"
)
def test_an_authored_v3_subworkflow_refuses_before_start_writes(
    storage: tuple[DbosRuntime, DbosDurableRunStarter],
) -> None:
    runtime, starter = storage
    workflow = revision(V3_SUBWORKFLOW_DOCUMENT)
    publish_revision(runtime.engine, workflow)

    result = starter.start_published(
        StartPublishedRunRequestV2(
            RunId("v3/unexecutable-child"), workflow.revision_hash, AgentBindingSet(())
        )
    )

    assert isinstance(result, DurableRunFormatNotExecutable)
    for table in PRODUCT_TABLE_NAMES - {
        "atelier_schema_versions",
        "workflow_revisions",
    }:
        assert count(runtime.engine, table) == 0
    assert count(runtime.engine, "workflow_status") == 0


def test_identical_retry_returns_current_run_without_enqueueing_again(
    storage: tuple[DbosRuntime, DbosDurableRunStarter], monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, starter = storage
    first = start(runtime, starter)

    def unexpected_enqueue(*args: object, **kwargs: object) -> object:
        raise AssertionError("retry enqueued again")

    monkeypatch.setattr(DBOSClient, "enqueue_in_transaction", unexpected_enqueue)
    second = start(runtime, starter)

    assert second == first
    assert count(runtime.engine, "runs") == 1
    assert count(runtime.engine, "workflow_status") == 1


@pytest.mark.parametrize(
    ("state", "current_node", "terminal_hash"),
    [
        (RunState.WAITING_RECONCILIATION, V3_EFFECT_LINE_ACTION_NODE_ID, None),
        (RunState.COMPLETED, V3_EFFECT_LINE_WAIT_NODE_ID, "0" * 64),
    ],
)
def test_retry_after_progress_returns_the_current_run(
    storage: tuple[DbosRuntime, DbosDurableRunStarter],
    state: RunState,
    current_node: str,
    terminal_hash: str | None,
) -> None:
    runtime, starter = storage
    start(runtime, starter)
    with runtime.engine.begin() as connection:
        connection.execute(
            sa.text(
                "UPDATE runs SET state=:state, current_node_id=:current_node, "
                "terminal_hash=:terminal_hash"
            ),
            {
                "state": state.value,
                "current_node": current_node,
                "terminal_hash": terminal_hash,
            },
        )

    assert start(runtime, starter).state is state
    assert count(runtime.engine, "workflow_status") == 1


def test_conflicting_run_id_fails_without_mutation(
    storage: tuple[DbosRuntime, DbosDurableRunStarter],
) -> None:
    runtime, starter = storage
    original = start(runtime, starter)

    conflict = start_result(
        runtime,
        starter,
        document=WORKFLOW_DOCUMENT.replace(
            b"Draft the pull request", b"Draft another pull request"
        ),
    )

    assert isinstance(conflict, RunIdentityConflict)
    assert start(runtime, starter) == original
    assert count(runtime.engine, "workflow_revisions") == 2
    assert count(runtime.engine, "runs") == 1
    assert count(runtime.engine, "workflow_status") == 1


def test_different_runs_share_the_same_exact_revision(
    storage: tuple[DbosRuntime, DbosDurableRunStarter],
) -> None:
    runtime, starter = storage

    start(runtime, starter, "run-1")
    start(runtime, starter, "run-2")

    assert count(runtime.engine, "workflow_revisions") == 1
    assert count(runtime.engine, "runs") == 2
    assert count(runtime.engine, "workflow_status") == 2


def test_durable_hash_collision_fails_without_run_or_enqueue(
    storage: tuple[DbosRuntime, DbosDurableRunStarter],
) -> None:
    runtime, starter = storage
    with runtime.engine.begin() as connection:
        connection.execute(
            workflow_revisions.insert().values(
                revision_hash=revision().revision_hash.value,
                document=b"different durable bytes",
            )
        )

    result = start_published_run(
        RunId("run-1"), revision().revision_hash, None, starter
    )

    assert isinstance(result, DurableStateCorrupt)
    assert count(runtime.engine, "workflow_revisions") == 1
    assert count(runtime.engine, "runs") == 0
    assert count(runtime.engine, "workflow_status") == 0


@pytest.mark.parametrize("operation", ["UPDATE", "DELETE"])
def test_revision_document_cannot_be_updated_or_deleted(
    storage: tuple[DbosRuntime, DbosDurableRunStarter], operation: str
) -> None:
    runtime, starter = storage
    started = start(runtime, starter)
    statement = (
        "UPDATE workflow_revisions SET document=X'00' WHERE revision_hash=:hash"
        if operation == "UPDATE"
        else "DELETE FROM workflow_revisions WHERE revision_hash=:hash"
    )

    with pytest.raises(exc.IntegrityError), runtime.engine.begin() as connection:
        connection.execute(sa.text(statement), {"hash": started.revision_hash.value})

    with runtime.engine.connect() as connection:
        assert (
            connection.scalar(
                sa.select(workflow_revisions.c.document).where(
                    workflow_revisions.c.revision_hash == started.revision_hash.value
                )
            )
            == WORKFLOW_DOCUMENT
        )


@pytest.mark.parametrize("version", [0, 5])
def test_unsupported_schema_version_is_refused_without_mutation(
    tmp_path: Path, version: int
) -> None:
    database_path = tmp_path / "atelier.sqlite"
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "CREATE TABLE atelier_schema_versions(version INTEGER NOT NULL)"
        )
        connection.execute("INSERT INTO atelier_schema_versions VALUES(?)", (version,))
    before = database_path.read_bytes()
    engine = sa.create_engine(f"sqlite:///{database_path}")

    with pytest.raises(UnsupportedSchemaVersion):
        initialize_schema(engine)

    engine.dispose()
    assert database_path.read_bytes() == before


@pytest.mark.parametrize("version", [1, 2, 3])
def test_pre_release_schema_versions_require_no_mutating_runtime_migration(
    tmp_path: Path, version: int
) -> None:
    database_path = tmp_path / "atelier.sqlite"
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "CREATE TABLE atelier_schema_versions(version INTEGER PRIMARY KEY)"
        )
        connection.execute("INSERT INTO atelier_schema_versions VALUES(?)", (version,))
    before = database_path.read_bytes()
    engine = sa.create_engine(f"sqlite:///{database_path}")

    with pytest.raises(MigrationRequired):
        initialize_schema(engine)

    engine.dispose()
    assert database_path.read_bytes() == before


def test_current_schema_opens_idempotently(tmp_path: Path) -> None:
    runtime = recording_exact_runtime(
        DbosRuntimeSettings(
            tmp_path / "atelier.sqlite",
            "executor-A",
            agent_scratch_root=agent_scratch_root(tmp_path),
        ),
        LoopbackEffectAdapterFactory(
            tmp_path / "external.sqlite",
            AdapterRevision("loopback-v1"),
            EffectDestination("loopback-test"),
        ),
        PROVIDER_OUTPUT,
    )
    try:
        initialize_schema(runtime.engine)
        initialize_schema(runtime.engine)
        assert count(runtime.engine, "atelier_schema_versions") == 1
    finally:
        runtime.close()


def test_concurrent_first_schema_initializers_converge_on_version_thirteen(
    tmp_path: Path,
) -> None:
    participants = 4

    def initialize(database_path: Path, barrier: Barrier) -> list[int]:
        engine = create_canonical_engine(database_path)
        try:
            barrier.wait(timeout=5)
            initialize_schema(engine)
            with engine.connect() as connection:
                return list(
                    connection.execute(
                        sa.text("SELECT version FROM atelier_schema_versions")
                    ).scalars()
                )
        finally:
            engine.dispose()

    for round_number in range(8):
        database_path = tmp_path / f"atelier-{round_number}.sqlite"
        barrier = Barrier(participants)

        with ThreadPoolExecutor(max_workers=participants) as pool:
            results = list(
                pool.map(
                    initialize,
                    repeat(database_path, participants),
                    repeat(barrier, participants),
                )
            )

        assert results == [[SCHEMA_VERSION]] * participants


def test_initialized_runtime_can_execute_a_later_seeded_workflow(
    storage: tuple[DbosRuntime, DbosDurableRunStarter],
) -> None:
    runtime, starter = storage
    started = start(runtime, starter)

    runtime.launch()
    wait_for_run_state(runtime.engine, started.run_id, RunState.WAITING_INPUT)
    with runtime.engine.connect() as connection:
        assert (
            connection.scalar(sa.text("SELECT state FROM runs"))
            == RunState.WAITING_INPUT.value
        )


@pytest.mark.parametrize("application_version", ["", "   "])
def test_runtime_requires_a_nonempty_application_version(
    tmp_path: Path, application_version: str
) -> None:
    with pytest.raises(ValueError, match="application_version"):
        DbosRuntimeSettings(tmp_path / "atelier.sqlite", application_version)


def test_bootstrap_returns_configured_start_and_requires_the_exact_new_run_binding(
    storage: tuple[DbosRuntime, DbosDurableRunStarter],
) -> None:
    runtime, starter = storage
    started = start(runtime, starter)

    assert (
        bootstrap_run_binding(runtime.datasource, started.run_id, started.revision_hash)
        == V3_EFFECT_LINE_AGENT_NODE_ID
    )

    with pytest.raises(RuntimeError, match="exact durable run binding"):
        bootstrap_run_binding(
            runtime.datasource,
            started.run_id,
            WorkflowRevision(b"other").revision_hash,
        )

    with runtime.engine.begin() as connection:
        connection.execute(
            sa.text(
                "UPDATE runs SET state='WAITING_RECONCILIATION', "
                "current_node_id='publish', state_version=1"
            )
        )
    with pytest.raises(RuntimeError, match="exact new durable run"):
        bootstrap_run_binding(runtime.datasource, started.run_id, started.revision_hash)
    with runtime.engine.connect() as connection:
        assert (
            connection.scalar(sa.text("SELECT state FROM runs"))
            == RunState.WAITING_RECONCILIATION.value
        )
