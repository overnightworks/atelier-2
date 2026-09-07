"""Phase D1: one inspected proposal binds one exact run across every restart."""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace
from typing import Any, cast

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.sql.functions import Function

import atelier2.application.advance_queue as advance_queue_module
from atelier2.adapters.dbos import schema as schema_module
from atelier2.adapters.dbos.catalog_store import DbosCatalogStore
from atelier2.adapters.dbos.queue_projection_store import DbosQueueProjectionStore
from atelier2.adapters.dbos.runtime import (
    DbosRuntime,
    DbosRuntimeSettings,
    create_canonical_engine,
)
from atelier2.adapters.dbos.schema import (
    PRODUCT_SCHEMA_HANDOFF,
    SCHEMA_VERSION,
    V43_SCHEMA_HANDOFF,
    MigrationRequired,
    StoreMigrationRefused,
    initialize_schema,
    migrate_store,
    queue_launch_bindings,
)
from atelier2.adapters.loopback import LoopbackEffectAdapterFactory
from atelier2.adapters.yaml_workflows import parse_workflow_document
from atelier2.api.app import create_app
from atelier2.api.openapi import (
    API_PREFIX,
    PROJECT_QUEUE_POLICY_PATH,
    QUEUE_ADMISSIONS_PATH,
    QUEUE_ITEMS_PATH,
    QUEUE_PROPOSALS_PATH,
)
from atelier2.api.references import encode_public_project_reference
from atelier2.application.advance_queue import (
    QueueAdvanceCorrupt,
    QueueAdvanceOutcome,
    QueueAdvanceUnavailable,
    QueueItemBlocked,
    QueueItemRestarting,
    QueueItemRestartWithheld,
    QueueRunStarted,
)
from atelier2.application.import_project_source_issues import (
    ImportProjectSourceIssuesOutcome,
    ProjectSourceIssuesImported,
    import_project_source_issues,
)
from atelier2.application.queue_sweep_reads import validated_snapshot
from atelier2.application.refusals import (
    DurableStateCorrupt as ApplicationDurableStateCorrupt,
)
from atelier2.application.refusals import SourcePayloadMalformed, WriteUnavailable
from atelier2.application.start_published_run import RunCreated
from atelier2.contracts.catalog_v3 import (
    CatalogActivatedAt,
    CatalogActor,
    CatalogLineageDisplayName,
    CatalogLineageId,
)
from atelier2.contracts.effects import AdapterRevision, EffectDestination
from atelier2.contracts.host_configuration import ProjectId
from atelier2.contracts.pages import MAXIMUM_PAGE_ITEMS
from atelier2.contracts.queue_projection import (
    MAXIMUM_QUEUE_ITEM_TITLE_CHARACTERS,
    MAXIMUM_QUEUE_LAUNCH_RESTARTS,
    ConfirmQueueProposal,
    PlanQueueItem,
    QueueAdmissionAlreadyCurrent,
    QueueAdmissionAuthorityRefused,
    QueueAdmissionRationale,
    QueueAutomationDisposition,
    QueueBlockerKind,
    QueueDecisionAuthority,
    QueueItemAdmitted,
    QueueItemId,
    QueueItemProposed,
    QueueItemSnapshot,
    QueueItemState,
    QueueItemTrackerObservation,
    QueueLaunchBinding,
    QueuePriorityRank,
    QueueProjectionRevision,
    QueueProjectPolicyDefaults,
    QueueProjectPolicyRevision,
    QueueProposal,
    QueueProposalAlreadyCurrent,
    QueueProposalRefusal,
    QueueProposalRefused,
    QueueProposalRevisionConflict,
    QueueProposalSource,
    QueueRestartRefusal,
    ReleaseQueueLaunch,
    TrackerItemReference,
    WorkItemReference,
)
from atelier2.contracts.revisions_v3 import PublishedRevision, RevisionKind
from atelier2.contracts.runs import (
    Run,
    RunId,
    RunState,
    WorkflowRevision,
    WorkflowRevisionHash,
)
from atelier2.contracts.when import RecordedAt
from atelier2.contracts.workflow_formats import WorkflowFormatVersion
from atelier2.ports.durable_runs import (
    DurablePublishedRunStarter,
    DurableStateCorrupt,
    DurableWriteUnavailable,
)
from atelier2.ports.issue_observation import (
    ObservedOpenTrackerItem,
    OpenTrackerItemsObserved,
    TrackerItemSource,
    TrackerSourceUnavailable,
)
from atelier2.ports.published_revisions import (
    CatalogLineageFounded,
    PublishedRevisionsUnavailable,
)
from atelier2.ports.queue_projection import (
    QueueItemsPage,
    QueueItemsReconciled,
    QueueLaunchBlocked,
    QueueLaunchReleased,
    QueueLaunchReleaseRefused,
    QueueLaunchReserved,
    QueueLaunchRestartsExhausted,
    QueueLaunchRunEnded,
    QueueLaunchRunOpen,
    QueueProjectPolicyAbsent,
    QueueProjectPolicyFound,
    QueueProjectPolicyPublished,
    QueueReadUnavailable,
)
from tests.scenarios.api import (
    api_limits,
    api_ports,
    durable_api_client,
    event_poll_backoff,
)
from tests.scenarios.issue_observation import FakeTrackerItemSource
from tests.scenarios.runs import publish_revision

PROJECT = ProjectId("project1")
FIRST_READ = RecordedAt("2026-09-01T09:00:00Z")
SECOND_READ = RecordedAt("2026-09-02T09:00:00Z")
THIRD_READ = RecordedAt("2026-09-03T09:00:00Z")
BINDING_FREE_SCHEMA = PublishedRevision(RevisionKind.SCHEMA, b"true")
"""The one schema a wait-only document needs published before it is executable.

`evaluate_executability` resolves every reference a V3 document pins, including
a Wait's declared output schema, before the start admits it -- so a line with no
agent role binding still needs this one pinned revision published, which
`_found_lineage` does for every document it seats.
"""

BINDING_FREE_WORKFLOW = f"""format_version: 3
name: Binding-free wait line
nodes:
  - id: approve
    type: wait
    prompt: Add [2, 3].
    outputs:
      - name: approval
        schema: {{ref: approval-schema, revision: {BINDING_FREE_SCHEMA.revision_hash.value}}}
""".encode()
"""A wait-only document: startable without resolving any agent role binding.

No node here declares a role, so no agent executor is ever needed to admit it --
exactly what these queue-launch tests want to hold constant while they vary the
admission machinery around it. The bracketed pair inside the prompt plays the
role a differing operand pair once did: `.replace(...)` on it is what gives a
test a second, distinguishable revision of the same shape.
"""


def _runtime(
    database_path: Path,
    *,
    project_root: Path | None = None,
    tracker: TrackerItemSource | None = None,
) -> DbosRuntime:
    """The Serve runtime these tests launch, optionally serving one project.

    A named project root is what makes the sweep's admission half run at all:
    without a served project and a connected tracker there is no policy to read
    and no label to read it against, so the plain harness gets neither.
    """

    return DbosRuntime(
        DbosRuntimeSettings(
            database_path,
            "phase-d-admission-test",
            project_id=None if project_root is None else PROJECT,
            bootstrap_project_root=project_root,
        ),
        LoopbackEffectAdapterFactory(
            database_path.parent / "external.sqlite",
            AdapterRevision("loopback-v1"),
            EffectDestination("loopback-test"),
        ),
        tracker_item_source=tracker,
    )


def _found_lineage(
    engine: Engine, document: bytes = BINDING_FREE_WORKFLOW
) -> tuple[CatalogLineageId, WorkflowRevisionHash]:
    revision = WorkflowRevision(document)
    publish_revision(engine, revision)
    catalog = DbosCatalogStore(engine)
    catalog.publish_revision(BINDING_FREE_SCHEMA)
    published = PublishedRevision(RevisionKind.WORKFLOW, document)
    catalog.publish_revision(published)
    founded = catalog.found_lineage(
        published,
        CatalogLineageDisplayName(f"phase-d-{revision.revision_hash.value[:8]}"),
        CatalogActor("operator"),
        CatalogActivatedAt("2026-08-27T10:00:00Z"),
    )
    assert isinstance(founded, CatalogLineageFounded)
    return founded.lineage.lineage_id, revision.revision_hash


def _proposal(
    lineage_id: CatalogLineageId,
    prerequisites: tuple[QueueItemId, ...] = (),
    rank: int = 1,
    policy_revision: int | None = 1,
    disposition: QueueAutomationDisposition = (
        QueueAutomationDisposition.HUMAN_REQUIRED
    ),
) -> QueueProposal:
    return QueueProposal(
        QueuePriorityRank(rank),
        lineage_id,
        prerequisites,
        disposition,
        policy_revision,
    )


def _insert_dependency_proposal(
    engine: Engine,
    item: WorkItemReference,
    lineage_id: CatalogLineageId,
    prerequisite: WorkItemReference,
    revision: int,
) -> None:
    with engine.begin() as connection:
        connection.execute(
            schema_module.queue_proposal_revisions.insert().values(
                item_id=item.item_id.value,
                proposal_revision=revision,
                project_id=item.project.value,
                priority_rank=1,
                workflow_lineage_id=lineage_id.value,
                automation_disposition=QueueAutomationDisposition.HUMAN_REQUIRED.value,
                policy_revision=1,
                source=QueueProposalSource.OPERATOR.value,
            )
        )
        connection.execute(
            schema_module.queue_dependency_edges.insert().values(
                item_id=item.item_id.value,
                proposal_revision=revision,
                project_id=item.project.value,
                prerequisite_item_id=prerequisite.item_id.value,
            )
        )


def _seed_open_items(
    store: DbosQueueProjectionStore, *references: WorkItemReference
) -> None:
    """Seed rows the way an import does: one reconciliation of the open set.

    The project's rows that are already open are carried into the run, so
    seeding one item never retires an item a test seeded before it.
    """

    (project,) = {reference.project for reference in references}
    page = store.list_items(None, MAXIMUM_PAGE_ITEMS)
    assert isinstance(page, QueueItemsPage)
    already_open = tuple(
        item.item_reference
        for item in page.items
        if item.item_reference.project == project and item.retired_at is None
    )
    open_set = {
        reference.item_id: reference for reference in (*already_open, *references)
    }
    observed_at = RecordedAt("2026-08-27T10:00:00Z")
    reconciled = store.reconcile_open_items(
        project,
        tuple(
            (
                reference,
                QueueItemTrackerObservation(
                    f"open item {reference.tracker_item.value}", observed_at
                ),
            )
            for reference in open_set.values()
        ),
        observed_at,
    )
    assert isinstance(reconciled, QueueItemsReconciled)


def _prepare_proposed(
    store: DbosQueueProjectionStore,
    lineage_id: CatalogLineageId,
    tracker: str = "gh:79",
    prerequisites: tuple[QueueItemId, ...] = (),
    rank: int = 1,
    policy_revision: int | None = 1,
    disposition: QueueAutomationDisposition = (
        QueueAutomationDisposition.HUMAN_REQUIRED
    ),
) -> QueueItemProposed:
    reference = WorkItemReference(PROJECT, TrackerItemReference(tracker))
    _seed_open_items(store, reference)
    proposed = store.plan(
        PlanQueueItem(
            reference,
            _proposal(lineage_id, prerequisites, rank, policy_revision, disposition),
            QueueProjectionRevision(0),
        )
    )
    assert isinstance(proposed, QueueItemProposed)
    return proposed


def _prepare_admitted(
    store: DbosQueueProjectionStore,
    lineage_id: CatalogLineageId,
    tracker: str = "gh:79",
    prerequisites: tuple[QueueItemId, ...] = (),
    rank: int = 1,
    policy_revision: int | None = 1,
) -> WorkItemReference:
    proposed = _prepare_proposed(
        store, lineage_id, tracker, prerequisites, rank, policy_revision
    )
    admitted = store.confirm(
        ConfirmQueueProposal(
            proposed.item_reference,
            proposed.revision,
            QueueAdmissionRationale("operator approved the inspected proposal"),
        )
    )
    assert isinstance(admitted, QueueItemAdmitted)
    return proposed.item_reference


@pytest.fixture
def store(tmp_path: Path) -> Iterator[tuple[DbosQueueProjectionStore, Engine]]:
    engine = create_canonical_engine(tmp_path / "atelier.sqlite")
    initialize_schema(engine)
    try:
        yield DbosQueueProjectionStore(engine), engine
    finally:
        engine.dispose()


def _import(
    queue: DbosQueueProjectionStore,
    observed_at: RecordedAt,
    *open_items: tuple[str, str],
    project: ProjectId = PROJECT,
) -> ImportProjectSourceIssuesOutcome:
    """Run the real import against the real store for one tracker answer."""

    source = FakeTrackerItemSource(
        open_items_answer=OpenTrackerItemsObserved(
            tuple(
                ObservedOpenTrackerItem(TrackerItemReference(reference), title, ())
                for reference, title in open_items
            ),
            observed_at,
        )
    )
    return import_project_source_issues(project, source, queue)


def _snapshots_by_reference(
    queue: DbosQueueProjectionStore,
) -> dict[TrackerItemReference, QueueItemSnapshot]:
    page = queue.list_items(None, MAXIMUM_PAGE_ITEMS)
    assert isinstance(page, QueueItemsPage)
    return {item.item_reference.tracker_item: item for item in page.items}


def _launch_bindings(engine: Engine, item_id: QueueItemId) -> tuple[sa.RowMapping, ...]:
    with engine.connect() as connection:
        return tuple(
            connection.execute(
                sa.select(queue_launch_bindings).where(
                    queue_launch_bindings.c.item_id == item_id.value
                )
            )
            .mappings()
            .all()
        )


def _durable_observations(
    engine: Engine,
) -> dict[str, tuple[str | None, str | None, str | None]]:
    """The three ADR 0016 columns as they lie, for rows a snapshot cannot read."""

    with engine.connect() as connection:
        return {
            str(record["tracker_item_reference"]): (
                record["observed_title"],
                record["title_observed_at"],
                record["retired_at"],
            )
            for record in connection.execute(
                sa.select(
                    schema_module.queue_items.c.tracker_item_reference,
                    schema_module.queue_items.c.observed_title,
                    schema_module.queue_items.c.title_observed_at,
                    schema_module.queue_items.c.retired_at,
                )
            ).mappings()
        }


def _run_started(
    run_id: RunId,
    workflow_revision_hash: WorkflowRevisionHash,
    _bindings: object,
    _starter: object,
    **_kwargs: object,
) -> RunCreated:
    return RunCreated(
        Run(run_id, workflow_revision_hash, RunState.STARTED, "final", 0, 0)
    )


def test_an_import_writes_every_open_items_title_with_the_runs_read_time(
    store: tuple[DbosQueueProjectionStore, Engine],
) -> None:
    queue, _engine = store

    outcome = _import(
        queue,
        FIRST_READ,
        ("gh:79", "Queue the workshop"),
        ("gh:962", "Date the observation"),
    )

    assert outcome == ProjectSourceIssuesImported(observed=2, newly_observed=2)
    snapshots = _snapshots_by_reference(queue)
    assert snapshots[TrackerItemReference("gh:79")].observation == (
        QueueItemTrackerObservation("Queue the workshop", FIRST_READ)
    )
    assert snapshots[TrackerItemReference("gh:962")].observation == (
        QueueItemTrackerObservation("Date the observation", FIRST_READ)
    )
    assert [item.retired_at for item in snapshots.values()] == [None, None]


def test_an_item_missing_from_the_open_set_retires_at_the_runs_read_time(
    store: tuple[DbosQueueProjectionStore, Engine],
) -> None:
    queue, _engine = store
    _import(queue, FIRST_READ, ("gh:79", "Still open"), ("gh:962", "Closed later"))

    outcome = _import(queue, SECOND_READ, ("gh:79", "Still open"))

    assert outcome == ProjectSourceIssuesImported(observed=1, newly_observed=0)
    snapshots = _snapshots_by_reference(queue)
    assert snapshots[TrackerItemReference("gh:962")].retired_at == SECOND_READ
    assert snapshots[TrackerItemReference("gh:79")].retired_at is None


def test_a_re_observed_item_loses_its_retirement(
    store: tuple[DbosQueueProjectionStore, Engine],
) -> None:
    queue, _engine = store
    _import(queue, FIRST_READ, ("gh:79", "Open"))
    _import(queue, SECOND_READ)

    reopened = _import(queue, THIRD_READ, ("gh:79", "Reopened"))

    assert reopened == ProjectSourceIssuesImported(observed=1, newly_observed=0)
    (snapshot,) = _snapshots_by_reference(queue).values()
    assert snapshot.retired_at is None
    assert snapshot.observation == QueueItemTrackerObservation("Reopened", THIRD_READ)


@pytest.mark.parametrize(
    "title",
    ["", "x" * (MAXIMUM_QUEUE_ITEM_TITLE_CHARACTERS + 1)],
    ids=["empty", "overlong"],
)
def test_a_title_the_projection_cannot_hold_leaves_the_projection_untouched(
    store: tuple[DbosQueueProjectionStore, Engine], title: str
) -> None:
    queue, _engine = store
    _import(queue, FIRST_READ, ("gh:79", "Open"))

    outcome = _import(queue, SECOND_READ, ("gh:79", "Renamed"), ("gh:962", title))

    assert isinstance(outcome, SourcePayloadMalformed)
    assert "gh:962" in outcome.detail
    (snapshot,) = _snapshots_by_reference(queue).values()
    assert snapshot.observation == QueueItemTrackerObservation("Open", FIRST_READ)


def test_a_run_for_one_project_leaves_another_projects_items_open(
    store: tuple[DbosQueueProjectionStore, Engine],
) -> None:
    queue, _engine = store
    other_project = ProjectId("other-project")
    _import(queue, FIRST_READ, ("gh:79", "Served project item"))
    _import(queue, FIRST_READ, ("gh:5", "Other project item"), project=other_project)

    _import(queue, SECOND_READ)

    snapshots = _snapshots_by_reference(queue)
    assert snapshots[TrackerItemReference("gh:79")].retired_at == SECOND_READ
    assert snapshots[TrackerItemReference("gh:5")].retired_at is None


def test_a_retired_item_stays_visible_and_is_never_started(
    store: tuple[DbosQueueProjectionStore, Engine],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queue, engine = store
    lineage_id, _revision_hash = _found_lineage(engine)
    queue.put_policy(QueueProjectPolicyRevision(PROJECT, 1, 1, None), 0)
    reference = _prepare_admitted(queue, lineage_id)
    monkeypatch.setattr(advance_queue_module, "start_published_run", _run_started)

    _import(queue, SECOND_READ)

    (snapshot,) = _snapshots_by_reference(queue).values()
    assert snapshot.item_reference == reference
    assert snapshot.retired_at == SECOND_READ
    assert snapshot.state is QueueItemState.ADMITTED
    assert (
        advance_queue_module.advance_queue(
            queue,
            DbosCatalogStore(engine),
            cast(DurablePublishedRunStarter, object()),
            workflow_document_parser=parse_workflow_document,
        )
        == ()
    )


def test_a_run_failing_after_its_first_write_leaves_every_row_unchanged(
    store: tuple[DbosQueueProjectionStore, Engine],
) -> None:
    """The reconciliation is one transaction, proven by the store's own guard.

    A durable row whose tracker reference was tampered with -- written here
    with the identity trigger dropped, as only a defect could -- no longer
    matches the identity its `item_id` derives from, so the run fails loud on
    that item after it has already inserted the first one. The projection must
    come back without that insert.
    """

    queue, engine = store
    _import(queue, FIRST_READ, ("gh:962", "Second"))
    tampered = WorkItemReference(PROJECT, TrackerItemReference("gh:962"))
    with sqlite3.connect(str(engine.url.database)) as connection:
        connection.execute("DROP TRIGGER queue_items_identity_no_update")
        connection.execute(
            "UPDATE queue_items SET tracker_item_reference='gh:tampered' "
            "WHERE item_id=?",
            (tampered.item_id.value,),
        )

    outcome = _import(queue, SECOND_READ, ("gh:79", "First"), ("gh:962", "Renamed"))

    assert outcome == ApplicationDurableStateCorrupt()
    assert _durable_observations(engine) == {
        "gh:tampered": ("Second", FIRST_READ.value, None)
    }


def test_validated_snapshot_carries_the_observation_and_retirement_through() -> None:
    """The sweep's revalidation cannot silently drop a snapshot field.

    Regression for a real finding: a fixed positional reconstruction dropped
    `observation` and `retired_at` to None on every item it revalidated.
    """

    reference = WorkItemReference(PROJECT, TrackerItemReference("gh:79"))
    observation = QueueItemTrackerObservation(
        "Give the queue its last-observed title", RecordedAt("2026-09-01T12:00:00Z")
    )
    retired_at = RecordedAt("2026-09-02T09:00:00Z")
    item = QueueItemSnapshot(
        reference,
        QueueItemState.OBSERVED,
        QueueProjectionRevision(0),
        None,
        observation=observation,
        retired_at=retired_at,
    )

    validated = validated_snapshot(item)

    assert validated.observation == observation
    assert validated.retired_at == retired_at


def test_proposal_and_manual_confirmation_are_separate_typed_transitions(
    store: tuple[DbosQueueProjectionStore, Engine],
) -> None:
    queue, engine = store
    lineage_id, _revision_hash = _found_lineage(engine)
    reference = WorkItemReference(PROJECT, TrackerItemReference("gh:79"))
    _seed_open_items(queue, reference)
    queue.put_policy(QueueProjectPolicyRevision(PROJECT, 1, 1, None), 0)
    command = PlanQueueItem(
        reference, _proposal(lineage_id), QueueProjectionRevision(0)
    )

    proposed = queue.plan(command)
    repeated_proposal = queue.plan(command)
    stale_reference = WorkItemReference(PROJECT, TrackerItemReference("gh:80"))
    _seed_open_items(queue, stale_reference)
    stale = queue.plan(
        PlanQueueItem(
            stale_reference, _proposal(lineage_id), QueueProjectionRevision(9)
        )
    )

    assert isinstance(proposed, QueueItemProposed)
    assert repeated_proposal == QueueProposalAlreadyCurrent(
        reference, command.proposal, proposed.revision
    )
    assert isinstance(stale, QueueProposalRevisionConflict)
    admitted = queue.confirm(
        ConfirmQueueProposal(
            reference,
            proposed.revision,
            QueueAdmissionRationale("approved"),
        )
    )
    assert isinstance(admitted, QueueItemAdmitted)
    assert admitted.admission.authority is QueueDecisionAuthority.OPERATOR
    repeated_admission = queue.confirm(
        ConfirmQueueProposal(
            reference,
            proposed.revision,
            QueueAdmissionRationale("approved"),
        )
    )
    assert isinstance(repeated_admission, QueueAdmissionAlreadyCurrent)

    human_required_reference = WorkItemReference(PROJECT, TrackerItemReference("gh:81"))
    _seed_open_items(queue, human_required_reference)
    human_required_proposal = queue.plan(
        PlanQueueItem(
            human_required_reference,
            _proposal(lineage_id),
            QueueProjectionRevision(0),
        )
    )
    assert isinstance(human_required_proposal, QueueItemProposed)
    refused_automation = queue.confirm(
        ConfirmQueueProposal(
            human_required_reference,
            human_required_proposal.revision,
            QueueAdmissionRationale("automation attempted confirmation"),
            QueueDecisionAuthority.AUTOMATION_RULE,
        )
    )
    assert refused_automation == QueueAdmissionAuthorityRefused(
        QueueDecisionAuthority.AUTOMATION_RULE,
        QueueAutomationDisposition.HUMAN_REQUIRED,
    )


def test_current_fresh_shape_keeps_phase_d_vocabulary_exact(tmp_path: Path) -> None:
    database_path = tmp_path / "atelier.sqlite"
    engine = create_canonical_engine(database_path)
    initialize_schema(engine)
    engine.dispose()

    with sqlite3.connect(database_path) as connection:
        assert (
            schema_module._fingerprint_for_version(connection, SCHEMA_VERSION)
            == PRODUCT_SCHEMA_HANDOFF.fingerprint_sha256
        )
        table_names = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert {table.name for table in schema_module._PHASE_D_QUEUE_TABLES} <= (
            table_names
        )
        trigger_names = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'trigger'"
            )
        }
        assert set(schema_module._PHASE_D_QUEUE_IMMUTABILITY_TRIGGERS) <= trigger_names
        queue_shape = str(
            connection.execute(
                "SELECT sql FROM sqlite_master "
                "WHERE type = 'table' AND name = 'queue_items'"
            ).fetchone()[0]
        )
        assert {state.value for state in QueueItemState} <= set(queue_shape.split("'"))
    assert {blocker.value for blocker in QueueBlockerKind} == {
        "PRIORITY_UNSET",
        "HUMAN_REQUIRED",
        "PREREQUISITE_OPEN",
        "PREREQUISITE_FAILED",
        "CAP_REACHED",
        "BINDING_UNRESOLVED",
        "REQUIRED_ORDER_UNAVAILABLE",
        "START_REFUSED",
        "LEGACY_REVIEW_REQUIRED",
    }


def test_v44_check_rejects_a_sql_null_partial_admission(tmp_path: Path) -> None:
    database_path = tmp_path / "atelier.sqlite"
    engine = create_canonical_engine(database_path)
    initialize_schema(engine)
    engine.dispose()
    reference = WorkItemReference(PROJECT, TrackerItemReference("gh:partial-check"))
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "INSERT INTO queue_items "
            "(item_id, project_id, tracker_item_reference, state, state_version) "
            "VALUES (?, ?, ?, 'OBSERVED', 0)",
            (
                reference.item_id.value,
                reference.project.value,
                reference.tracker_item.value,
            ),
        )
        connection.execute("DROP TRIGGER queue_items_state_transition")
        with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
            connection.execute(
                "UPDATE queue_items SET state='ADMITTED', state_version=2, "
                "workflow_lineage_id=?, admission_rationale='approved', "
                "current_proposal_revision=1 WHERE item_id=?",
                ("a1" * 32, reference.item_id.value),
            )


def test_a_policy_row_cannot_hold_half_of_a_proposal_default(tmp_path: Path) -> None:
    """The typed policy cannot state half a default, and neither can the table.

    Written through raw SQL, because that is the only way the shape this
    constraint guards can be attempted at all.
    """

    database_path = tmp_path / "atelier.sqlite"
    engine = create_canonical_engine(database_path)
    initialize_schema(engine)
    engine.dispose()
    with sqlite3.connect(database_path) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
            connection.execute(
                "INSERT INTO queue_project_policy_revisions "
                "(project_id, revision_number, maximum_active_runs, "
                "default_priority_rank) VALUES (?, 1, 1, 4)",
                (PROJECT.value,),
            )
        connection.execute(
            "INSERT INTO queue_project_policy_revisions "
            "(project_id, revision_number, maximum_active_runs) VALUES (?, 1, 1)",
            (PROJECT.value,),
        )


def test_policy_and_launch_reservation_are_atomic_under_the_project_cap(
    store: tuple[DbosQueueProjectionStore, Engine],
) -> None:
    queue, engine = store
    lineage_id, revision_hash = _found_lineage(engine)
    policy = queue.put_policy(QueueProjectPolicyRevision(PROJECT, 1, 1, None), 0)
    assert isinstance(policy, QueueProjectPolicyPublished)
    references = tuple(
        _prepare_admitted(queue, lineage_id, f"gh:{number}") for number in (79, 80)
    )
    barrier = Barrier(2)

    def reserve(index: int) -> object:
        barrier.wait()
        reference = references[index]
        return queue.reserve_launch(
            QueueLaunchBinding(
                reference.item_id,
                QueueProjectionRevision(1),
                RunId(f"phase-d-run-{index}"),
                revision_hash,
            )
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = tuple(executor.map(reserve, range(2)))

    assert sum(isinstance(outcome, QueueLaunchReserved) for outcome in outcomes) == 1
    blocked = next(
        outcome for outcome in outcomes if isinstance(outcome, QueueLaunchBlocked)
    )
    assert QueueBlockerKind.CAP_REACHED in blocked.item.blockers


def test_a_policy_less_project_reserves_and_launches_its_admitted_item(
    store: tuple[DbosQueueProjectionStore, Engine],
) -> None:
    """No published policy revision means no cap, not corruption (ruling 28.08.2026)."""
    queue, engine = store
    lineage_id, revision_hash = _found_lineage(engine)
    reference = _prepare_admitted(
        queue, lineage_id, "gh:no-policy", policy_revision=None
    )

    result = queue.reserve_launch(
        QueueLaunchBinding(
            reference.item_id,
            QueueProjectionRevision(1),
            RunId("no-policy-run"),
            revision_hash,
        )
    )

    assert isinstance(result, QueueLaunchReserved)
    with engine.connect() as connection:
        assert (
            connection.scalar(
                sa.select(sa.func.count()).select_from(queue_launch_bindings)
            )
            == 1
        )


def test_unreadable_capacity_count_fails_the_reservation_loud(
    store: tuple[DbosQueueProjectionStore, Engine],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queue, engine = store
    lineage_id, revision_hash = _found_lineage(engine)
    queue.put_policy(QueueProjectPolicyRevision(PROJECT, 1, 1, None), 0)
    reference = _prepare_admitted(queue, lineage_id)
    scalar = cast(Any, sa.engine.Connection.scalar)

    def unreadable_count(
        connection: sa.engine.Connection, statement: Any, *args: Any, **kwargs: Any
    ) -> Any:
        if any(
            isinstance(element, Function) and element.name.lower() == "count"
            for element in sa.sql.visitors.iterate(statement)
        ):
            return None
        return scalar(connection, statement, *args, **kwargs)

    monkeypatch.setattr(sa.engine.Connection, "scalar", unreadable_count)

    result = queue.reserve_launch(
        QueueLaunchBinding(
            reference.item_id,
            QueueProjectionRevision(1),
            RunId("unreadable-count-run"),
            revision_hash,
        )
    )

    assert isinstance(result, DurableStateCorrupt)


def test_dependencies_require_completed_and_ready_items_order_by_rank_then_id(
    store: tuple[DbosQueueProjectionStore, Engine],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queue, engine = store
    lineage_id, revision_hash = _found_lineage(engine)
    queue.put_policy(QueueProjectPolicyRevision(PROJECT, 1, 3, None), 0)
    prerequisite = _prepare_admitted(queue, lineage_id, "gh:prerequisite", rank=3)
    dependent = _prepare_admitted(
        queue,
        lineage_id,
        "gh:dependent",
        (prerequisite.item_id,),
        rank=1,
    )
    peer = _prepare_admitted(queue, lineage_id, "gh:peer", rank=1)
    prerequisite_run = RunId("phase-d-prerequisite")
    assert isinstance(
        queue.reserve_launch(
            QueueLaunchBinding(
                prerequisite.item_id,
                QueueProjectionRevision(1),
                prerequisite_run,
                revision_hash,
            )
        ),
        QueueLaunchReserved,
    )
    with engine.begin() as connection:
        connection.execute(
            schema_module.runs.insert().values(
                run_id=prerequisite_run.value,
                bootstrap_workflow_id="phase-d-prerequisite-bootstrap",
                revision_hash=revision_hash.value,
                workflow_format_version=1,
                current_node_id="final",
                current_round_ordinal=1,
                state=RunState.STARTED.value,
                state_version=0,
                last_event_sequence=0,
                terminal_hash=None,
            )
        )
    page = queue.list_items(None, 50)
    assert isinstance(page, QueueItemsPage)
    open_dependent = next(
        item for item in page.items if item.item_reference == dependent
    )
    assert open_dependent.blockers == (QueueBlockerKind.PREREQUISITE_OPEN,)
    with engine.begin() as connection:
        connection.execute(
            schema_module.runs.update()
            .where(schema_module.runs.c.run_id == prerequisite_run.value)
            .values(
                state=RunState.COMPLETED.value,
                state_version=1,
                terminal_hash="ab" * 32,
            )
        )
    page = queue.list_items(None, 50)
    assert isinstance(page, QueueItemsPage)
    completed_dependent = next(
        item for item in page.items if item.item_reference == dependent
    )
    assert completed_dependent.blockers == ()

    def started(
        run_id: RunId,
        workflow_revision_hash: WorkflowRevisionHash,
        _bindings: object,
        _starter: object,
        **_kwargs: object,
    ) -> RunCreated:
        return RunCreated(
            Run(run_id, workflow_revision_hash, RunState.STARTED, "final", 0, 0)
        )

    monkeypatch.setattr(advance_queue_module, "start_published_run", started)
    outcomes = advance_queue_module.advance_queue(
        queue,
        DbosCatalogStore(engine),
        cast(DurablePublishedRunStarter, object()),
        workflow_document_parser=parse_workflow_document,
    )
    started_items = [
        outcome.item_id for outcome in outcomes if isinstance(outcome, QueueRunStarted)
    ]
    assert started_items == [
        *sorted((dependent.item_id, peer.item_id), key=lambda item_id: item_id.value),
        prerequisite.item_id,
    ]


def test_list_items_pages_seek_by_the_start_order_key_not_by_item_id(
    store: tuple[DbosQueueProjectionStore, Engine],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A page walk survives item ids whose hash order disagrees with rank order.

    `gh:27` hashes below `gh:22` and `gh:26` yet carries the best (lowest)
    rank, so a seek that continued past the raw `item_id` boundary -- rather
    than behind the ordering key `advance_queue` and the list share -- would
    skip or repeat an item once a one-item page forces the boundary between
    them (Grok pre-review, #1051). The items `advance_queue` starts must also
    come out of the walk as a subsequence, in the order it started them. One
    item (`gh:22`) is retired through the production import path partway
    through, proving ADR 0016's split: the list keeps it in its ordered
    place, the start walk does not.
    """

    queue, engine = store
    lineage_id, _revision_hash = _found_lineage(engine)
    queue.put_policy(QueueProjectPolicyRevision(PROJECT, 1, 10, None), 0)
    best = _prepare_admitted(queue, lineage_id, "gh:27", rank=1)
    middle = _prepare_admitted(queue, lineage_id, "gh:22", rank=2)
    worst = _prepare_admitted(queue, lineage_id, "gh:26", rank=3)
    unranked = WorkItemReference(PROJECT, TrackerItemReference("gh:23"))
    _seed_open_items(queue, unranked)

    retirement = _import(
        queue,
        SECOND_READ,
        ("gh:27", "Best"),
        ("gh:26", "Worst"),
        ("gh:23", "Unranked"),
    )
    assert isinstance(retirement, ProjectSourceIssuesImported)

    listed_items: list[QueueItemSnapshot] = []
    after: QueueItemId | None = None
    for _ in range(10):
        page = queue.list_items(after, 1)
        assert isinstance(page, QueueItemsPage)
        assert len(page.items) == 1
        listed_items.append(page.items[0])
        if page.next_after is None:
            break
        after = page.next_after
    else:
        pytest.fail("the page walk did not terminate")

    listed = [item.item_reference.item_id for item in listed_items]
    assert listed == [best.item_id, middle.item_id, worst.item_id, unranked.item_id]
    (retired_middle,) = (item for item in listed_items if item.item_reference == middle)
    assert retired_middle.retired_at is not None

    monkeypatch.setattr(advance_queue_module, "start_published_run", _run_started)
    outcomes = advance_queue_module.advance_queue(
        queue,
        DbosCatalogStore(engine),
        cast(DurablePublishedRunStarter, object()),
        workflow_document_parser=parse_workflow_document,
    )
    started_items = [
        outcome.item_id for outcome in outcomes if isinstance(outcome, QueueRunStarted)
    ]
    assert started_items == [best.item_id, worst.item_id]
    assert [listed.index(item_id) for item_id in started_items] == sorted(
        listed.index(item_id) for item_id in started_items
    )


def test_a_page_walk_repeats_an_item_that_gains_a_proposal_between_pages(
    store: tuple[DbosQueueProjectionStore, Engine],
) -> None:
    """The seek's one honest edge: the after-item's key can move earlier mid-walk.

    `_queue_start_order_key` reads the after-item's *current* key fresh for
    every page, not the key the previous page served it under. The only
    reachable key change in this codebase is an OBSERVED item (no proposal,
    sorted last) gaining a proposal via `QueueItemSnapshot.plan` and becoming
    PROPOSED (ranked, sorted by `priority.rank`) -- always earlier, since a
    planned proposal can never be re-planned or withdrawn (`plan` refuses a
    second call once PROPOSED or ADMITTED). Read fresh, the after-item then
    seeks from its new, earlier position: an item that used to sort between
    the two positions is served again (a repeat). Nothing moves an item
    *later* in this codebase, so a skip is not a reachable outcome here; this
    test pins the one direction that is.
    """

    queue, engine = store
    lineage_id, _revision_hash = _found_lineage(engine)
    queue.put_policy(QueueProjectPolicyRevision(PROJECT, 1, 10, None), 0)
    ranked = _prepare_admitted(queue, lineage_id, "gh:ranked", rank=5)
    after_item = WorkItemReference(PROJECT, TrackerItemReference("gh:after-item"))
    later_item = WorkItemReference(PROJECT, TrackerItemReference("gh:later-item"))
    _seed_open_items(queue, after_item, later_item)

    first_page = queue.list_items(None, 2)
    assert isinstance(first_page, QueueItemsPage)
    assert [item.item_reference.item_id for item in first_page.items] == [
        ranked.item_id,
        after_item.item_id,
    ]
    assert first_page.next_after == after_item.item_id

    promoted = queue.plan(
        PlanQueueItem(
            after_item, _proposal(lineage_id, rank=1), QueueProjectionRevision(0)
        )
    )
    assert isinstance(promoted, QueueItemProposed)

    second_page = queue.list_items(first_page.next_after, 2)

    assert isinstance(second_page, QueueItemsPage)
    assert [item.item_reference.item_id for item in second_page.items] == [
        ranked.item_id,
        later_item.item_id,
    ]
    assert second_page.next_after is None


@pytest.mark.parametrize("proposal_revision", [None, 1])
def test_phase_d_state_without_its_proposal_fails_loud(
    store: tuple[DbosQueueProjectionStore, Engine], proposal_revision: int | None
) -> None:
    queue, engine = store
    reference = WorkItemReference(PROJECT, TrackerItemReference("gh:corrupt-proposed"))
    _seed_open_items(queue, reference)
    with sqlite3.connect(str(engine.url.database)) as connection:
        connection.execute("DROP TRIGGER queue_items_state_transition")
        connection.execute("PRAGMA ignore_check_constraints=ON")
        connection.execute(
            "UPDATE queue_items SET state='PROPOSED', state_version=1, "
            "current_proposal_revision=? WHERE item_id=?",
            (proposal_revision, reference.item_id.value),
        )
        connection.commit()

    assert isinstance(queue.list_items(None, 50), DurableStateCorrupt)


def test_unknown_prerequisite_run_state_fails_loud(
    store: tuple[DbosQueueProjectionStore, Engine],
) -> None:
    queue, engine = store
    lineage_id, revision_hash = _found_lineage(engine)
    queue.put_policy(QueueProjectPolicyRevision(PROJECT, 1, 2, None), 0)
    prerequisite = _prepare_admitted(queue, lineage_id, "gh:corrupt-run")
    _prepare_admitted(
        queue, lineage_id, "gh:dependent-on-corrupt-run", (prerequisite.item_id,)
    )
    run_id = RunId("corrupt-prerequisite-run")
    assert isinstance(
        queue.reserve_launch(
            QueueLaunchBinding(
                prerequisite.item_id,
                QueueProjectionRevision(1),
                run_id,
                revision_hash,
            )
        ),
        QueueLaunchReserved,
    )
    with engine.begin() as connection:
        connection.execute(
            schema_module.runs.insert().values(
                run_id=run_id.value,
                bootstrap_workflow_id="corrupt-prerequisite-bootstrap",
                revision_hash=revision_hash.value,
                workflow_format_version=1,
                current_node_id="final",
                current_round_ordinal=1,
                state=RunState.STARTED.value,
                state_version=0,
                last_event_sequence=0,
                terminal_hash=None,
            )
        )
        connection.exec_driver_sql("PRAGMA ignore_check_constraints=ON")
        connection.execute(
            schema_module.runs.update()
            .where(schema_module.runs.c.run_id == run_id.value)
            .values(state="UNKNOWN_DURABLE_STATE")
        )

    assert isinstance(queue.list_items(None, 50), DurableStateCorrupt)


def test_plan_fails_loud_when_derived_identity_collides_with_another_reference(
    store: tuple[DbosQueueProjectionStore, Engine],
) -> None:
    queue, engine = store
    lineage_id, _revision_hash = _found_lineage(engine)
    reference = WorkItemReference(PROJECT, TrackerItemReference("gh:identity"))
    with engine.begin() as connection:
        connection.execute(
            schema_module.queue_items.insert().values(
                item_id=reference.item_id.value,
                project_id="different-project",
                tracker_item_reference="gh:different",
                state=QueueItemState.OBSERVED.value,
                state_version=0,
            )
        )

    result = queue.plan(
        PlanQueueItem(reference, _proposal(lineage_id), QueueProjectionRevision(0))
    )

    assert isinstance(result, DurableStateCorrupt)


def test_dependency_cycle_check_ignores_superseded_same_project_edges(
    store: tuple[DbosQueueProjectionStore, Engine],
) -> None:
    queue, engine = store
    lineage_id, _revision_hash = _found_lineage(engine)
    queue.put_policy(QueueProjectPolicyRevision(PROJECT, 1, 1, None), 0)
    first = WorkItemReference(PROJECT, TrackerItemReference("gh:first"))
    second = WorkItemReference(PROJECT, TrackerItemReference("gh:second"))
    _seed_open_items(queue, first, second)
    assert isinstance(
        queue.plan(
            PlanQueueItem(
                first,
                QueueProposal(
                    QueuePriorityRank(1),
                    lineage_id,
                    (second.item_id,),
                    QueueAutomationDisposition.HUMAN_REQUIRED,
                    1,
                ),
                QueueProjectionRevision(0),
            )
        ),
        QueueItemProposed,
    )
    _insert_dependency_proposal(engine, second, lineage_id, first, 99)
    command_reference = WorkItemReference(PROJECT, TrackerItemReference("gh:no-deps"))

    result = queue.plan(
        PlanQueueItem(
            command_reference,
            _proposal(lineage_id),
            QueueProjectionRevision(0),
        )
    )

    assert isinstance(result, QueueItemProposed)


def test_dependency_cycle_check_ignores_current_cross_project_edges(
    store: tuple[DbosQueueProjectionStore, Engine],
) -> None:
    queue, engine = store
    lineage_id, _revision_hash = _found_lineage(engine)
    other_project = ProjectId("other-project")
    queue.put_policy(QueueProjectPolicyRevision(PROJECT, 1, 1, None), 0)
    queue.put_policy(QueueProjectPolicyRevision(other_project, 1, 1, None), 0)
    first = WorkItemReference(other_project, TrackerItemReference("gh:first"))
    second = WorkItemReference(other_project, TrackerItemReference("gh:second"))
    _seed_open_items(queue, first, second)
    assert isinstance(
        queue.plan(
            PlanQueueItem(
                first,
                QueueProposal(
                    QueuePriorityRank(1),
                    lineage_id,
                    (second.item_id,),
                    QueueAutomationDisposition.HUMAN_REQUIRED,
                    1,
                ),
                QueueProjectionRevision(0),
            )
        ),
        QueueItemProposed,
    )
    _insert_dependency_proposal(engine, second, lineage_id, first, 1)
    with engine.begin() as connection:
        connection.execute(
            schema_module.queue_items.update()
            .where(schema_module.queue_items.c.item_id == second.item_id.value)
            .values(
                state=QueueItemState.PROPOSED.value,
                state_version=1,
                current_proposal_revision=1,
            )
        )
    command_reference = WorkItemReference(PROJECT, TrackerItemReference("gh:no-deps"))

    result = queue.plan(
        PlanQueueItem(
            command_reference,
            _proposal(lineage_id),
            QueueProjectionRevision(0),
        )
    )

    assert isinstance(result, QueueItemProposed)


def test_queue_proposal_refusals_are_closed_typed_decisions(
    store: tuple[DbosQueueProjectionStore, Engine],
) -> None:
    queue, engine = store
    lineage_id, _revision_hash = _found_lineage(engine)
    queue.put_policy(QueueProjectPolicyRevision(PROJECT, 1, 1, None), 0)

    def plan(
        tracker: str,
        prerequisites: tuple[QueueItemId, ...],
        policy_revision: int = 1,
    ) -> object:
        reference = WorkItemReference(PROJECT, TrackerItemReference(tracker))
        return queue.plan(
            PlanQueueItem(
                reference,
                QueueProposal(
                    QueuePriorityRank(1),
                    lineage_id,
                    prerequisites,
                    QueueAutomationDisposition.HUMAN_REQUIRED,
                    policy_revision,
                ),
                QueueProjectionRevision(0),
            )
        )

    self_reference = WorkItemReference(PROJECT, TrackerItemReference("gh:self"))
    self_refusal = queue.plan(
        PlanQueueItem(
            self_reference,
            QueueProposal(
                QueuePriorityRank(1),
                lineage_id,
                (self_reference.item_id,),
                QueueAutomationDisposition.HUMAN_REQUIRED,
                1,
            ),
            QueueProjectionRevision(0),
        )
    )
    missing_policy = plan("gh:policy-missing", (), 99)
    unpublished_lineage = queue.plan(
        PlanQueueItem(
            WorkItemReference(PROJECT, TrackerItemReference("gh:lineage-missing")),
            _proposal(CatalogLineageId("ab" * 32)),
            QueueProjectionRevision(0),
        )
    )
    missing_prerequisite = plan("gh:prerequisite-missing", (QueueItemId("c3" * 32),))
    other_project_reference = WorkItemReference(
        ProjectId("other-project"), TrackerItemReference("gh:outside-project")
    )
    _seed_open_items(queue, other_project_reference)
    outside_project = plan("gh:outside-dependent", (other_project_reference.item_id,))
    first = WorkItemReference(PROJECT, TrackerItemReference("gh:cycle-first"))
    second = WorkItemReference(PROJECT, TrackerItemReference("gh:cycle-second"))
    _seed_open_items(queue, first, second)
    assert isinstance(
        queue.plan(
            PlanQueueItem(
                first,
                _proposal(lineage_id, (second.item_id,)),
                QueueProjectionRevision(0),
            )
        ),
        QueueItemProposed,
    )
    cycle = queue.plan(
        PlanQueueItem(
            second,
            _proposal(lineage_id, (first.item_id,)),
            QueueProjectionRevision(0),
        )
    )

    assert self_refusal == QueueProposalRefused(QueueProposalRefusal.SELF_DEPENDENCY)
    assert missing_policy == QueueProposalRefused(
        QueueProposalRefusal.POLICY_REVISION_MISSING
    )
    assert unpublished_lineage == QueueProposalRefused(
        QueueProposalRefusal.WORKFLOW_LINEAGE_MISSING
    )
    assert missing_prerequisite == QueueProposalRefused(
        QueueProposalRefusal.PREREQUISITE_NOT_IN_PROJECT
    )
    assert outside_project == QueueProposalRefused(
        QueueProposalRefusal.PREREQUISITE_NOT_IN_PROJECT
    )
    assert cycle == QueueProposalRefused(QueueProposalRefusal.DEPENDENCY_CYCLE)
    api_reference = WorkItemReference(PROJECT, TrackerItemReference("gh:api-self"))
    with _queue_api(queue) as api:
        response = api.put(
            QUEUE_PROPOSALS_PATH,
            json={
                "project_id": PROJECT.value,
                "tracker_item_reference": api_reference.tracker_item.value,
                "expected_revision": 0,
                "priority": {"rank": 1},
                "workflow_lineage_id": lineage_id.value,
                "prerequisite_item_ids": [api_reference.item_id.value],
                "automation_disposition": "HUMAN_REQUIRED",
                "policy_revision": 1,
            },
        )
    assert response.status_code == 422
    assert response.json()["type"].endswith("queue-proposal-refused")


def _restore_v43(database_path: Path) -> None:
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute("BEGIN IMMEDIATE")
        # V51 added the permission ledger; a V43 store predates it.
        for trigger in schema_module._PERMISSION_RECEIPT_TRIGGERS:
            connection.execute(f"DROP TRIGGER IF EXISTS {trigger}")
        connection.execute(
            f"DROP TABLE IF EXISTS {schema_module.permission_receipts.name}"
        )
        # V54 widened the effect operation vocabulary; a V43 store predates it.
        for table, parked, triggers in (
            (
                schema_module.effect_receipts,
                "effect_receipts_v54",
                ("effect_receipts_no_update", "effect_receipts_no_delete"),
            ),
            (
                schema_module.effect_intents,
                "effect_intents_v54",
                (
                    "effect_intents_binding_no_update",
                    "effect_intents_no_delete",
                    "effect_intents_abandonment",
                    "effect_intents_no_abandoned_insert",
                ),
            ),
        ):
            schema_module._rebuild_product_table(
                connection,
                table,
                parked,
                triggers,
                schema_module.SCHEMA_VERSION,
                53,
            )
        # V53 widened the attempt failure vocabulary; a V43 store predates it.
        schema_module._rebuild_product_table(
            connection,
            schema_module.agent_attempts,
            "agent_attempts_v53",
            schema_module._AGENT_ATTEMPTS_TRIGGERS,
            53,
            52,
            trigger_source=schema_module._V52_AGENT_ATTEMPT_TRIGGERS,
        )
        # V50 widened the attempt failure vocabulary; a V43 store predates it.
        schema_module._rebuild_product_table(
            connection,
            schema_module.agent_attempts,
            "agent_attempts_v50",
            schema_module._AGENT_ATTEMPTS_TRIGGERS,
            50,
            49,
            trigger_source=schema_module._V49_AGENT_ATTEMPT_TRIGGERS,
        )
        for trigger in schema_module._DEFINITION_SOURCE_TRIGGERS:
            connection.execute(f"DROP TRIGGER IF EXISTS {trigger}")
        for table in reversed(schema_module._DEFINITION_SOURCE_TABLES):
            connection.execute(f"DROP TABLE IF EXISTS {table.name}")
        for trigger in ("catalog_intakes_no_update", "catalog_intakes_no_delete"):
            connection.execute(f"DROP TRIGGER IF EXISTS {trigger}")
        connection.execute("DROP TABLE IF EXISTS catalog_intakes")
        schema_module._rebuild_product_table(
            connection,
            schema_module.run_events,
            "run_events_v46",
            schema_module._RUN_EVENTS_TRIGGERS,
            46,
            45,
        )
        schema_module._rebuild_product_table(
            connection,
            schema_module.wait_answers,
            "wait_answers_v46",
            schema_module._WAIT_ANSWERS_TRIGGERS,
            46,
            45,
            trigger_source=schema_module.PUBLISHED_WAIT_ANSWER_TRIGGERS[45],
        )
        schema_module._rebuild_product_table(
            connection,
            schema_module.host_project_source_connection_revisions,
            "project_source_connections_v45",
            (
                "host_project_source_connection_revisions_no_update",
                "host_project_source_connection_revisions_no_delete",
            ),
            45,
            44,
        )
        schema_module._rebuild_product_table(
            connection,
            schema_module.queue_items,
            "queue_items_v44",
            ("queue_items_identity_no_update", "queue_items_no_delete"),
            44,
            43,
        )
        for table in reversed(schema_module._PHASE_D_QUEUE_TABLES):
            connection.execute(f"DROP TABLE {table.name}")
        connection.execute("UPDATE atelier_schema_versions SET version = 43")
        connection.commit()
        schema_module._require_product_shape(connection, 43)


def _logical_dump(database_path: Path) -> tuple[str, ...]:
    with sqlite3.connect(database_path) as connection:
        return tuple(connection.iterdump())


@pytest.mark.proves("the-v43-hop-invents-no-queue-decision")
def test_v43_to_v44_preserves_populated_rows_and_invents_no_queue_decision(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "atelier.sqlite"
    engine = create_canonical_engine(database_path)
    initialize_schema(engine)
    lineage_id, _revision_hash = _found_lineage(engine)
    engine.dispose()
    _restore_v43(database_path)
    reference = WorkItemReference(PROJECT, TrackerItemReference("gh:legacy"))
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "INSERT INTO queue_items VALUES (?, ?, ?, 'ADMITTED', 1, ?, ?)",
            (
                reference.item_id.value,
                PROJECT.value,
                reference.tracker_item.value,
                lineage_id.value,
                "legacy approval",
            ),
        )
        connection.commit()

    report = migrate_store(database_path)

    assert report.source_version == V43_SCHEMA_HANDOFF.version
    assert report.target_version == SCHEMA_VERSION == 55
    assert report.fingerprint_sha256 == PRODUCT_SCHEMA_HANDOFF.fingerprint_sha256
    reopened = create_canonical_engine(database_path)
    try:
        page = DbosQueueProjectionStore(reopened).list_items(None, 50)
        assert isinstance(page, QueueItemsPage)
        (legacy,) = page.items
        assert legacy.state is QueueItemState.ADMITTED
        assert legacy.admission is not None
        assert legacy.admission.rationale.value == "legacy approval"
        assert legacy.proposal is None
        assert legacy.launch_binding is None
        assert legacy.blockers == (QueueBlockerKind.LEGACY_REVIEW_REQUIRED,)
        (outcome,) = advance_queue_module.advance_queue(
            DbosQueueProjectionStore(reopened),
            DbosCatalogStore(reopened),
            cast(DurablePublishedRunStarter, object()),
            workflow_document_parser=parse_workflow_document,
        )
        assert isinstance(outcome, QueueItemBlocked)
        assert outcome.blockers == (QueueBlockerKind.LEGACY_REVIEW_REQUIRED,)
        with reopened.connect() as connection:
            assert (
                connection.scalar(
                    sa.select(sa.func.count()).select_from(queue_launch_bindings)
                )
                == 0
            )
    finally:
        reopened.dispose()


def test_v44_migration_collision_and_failpoint_roll_back_exactly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    collision_path = tmp_path / "collision.sqlite"
    engine = create_canonical_engine(collision_path)
    initialize_schema(engine)
    engine.dispose()
    _restore_v43(collision_path)
    with sqlite3.connect(collision_path) as connection:
        connection.execute("CREATE TABLE queue_items_before_phase_d (held INTEGER)")
        connection.commit()
    before_collision = _logical_dump(collision_path)
    with pytest.raises(StoreMigrationRefused, match="queue_items_before_phase_d"):
        migrate_store(collision_path)
    assert _logical_dump(collision_path) == before_collision

    failpoint_path = tmp_path / "failpoint.sqlite"
    engine = create_canonical_engine(failpoint_path)
    initialize_schema(engine)
    engine.dispose()
    _restore_v43(failpoint_path)
    before_failpoint = _logical_dump(failpoint_path)
    original = schema_module._SCHEMA_MIGRATION_BY_SOURCE[43]

    def fail_after_step(connection: sqlite3.Connection) -> None:
        original.apply(connection)
        raise sqlite3.OperationalError("v44-after-version-cas-failpoint")

    monkeypatch.setitem(
        schema_module._SCHEMA_MIGRATION_BY_SOURCE,
        43,
        replace(original, apply=fail_after_step),
    )
    with pytest.raises(StoreMigrationRefused, match="v44-after-version-cas-failpoint"):
        migrate_store(failpoint_path)
    assert _logical_dump(failpoint_path) == before_failpoint


def test_runtime_refuses_an_unmigrated_v43_store(tmp_path: Path) -> None:
    database_path = tmp_path / "atelier.sqlite"
    engine = create_canonical_engine(database_path)
    initialize_schema(engine)
    engine.dispose()
    _restore_v43(database_path)

    with pytest.raises(MigrationRequired):
        initialize_schema(create_canonical_engine(database_path))


@pytest.mark.parametrize("crash_after_start", [False, True])
@pytest.mark.proves("a-manually-approved-queue-item-starts-once")
def test_one_manually_approved_item_starts_once_across_a_crash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    crash_after_start: bool,
) -> None:
    database_path = tmp_path / "atelier.sqlite"
    first = _runtime(database_path)
    first.initialize_storage()
    lineage_id, _revision_hash = _found_lineage(first.engine)
    queue = DbosQueueProjectionStore(first.engine)
    queue.put_policy(QueueProjectPolicyRevision(PROJECT, 1, 1, None), 0)
    reference = _prepare_admitted(queue, lineage_id)
    original_start = advance_queue_module.start_published_run

    def crash(*args: Any, **kwargs: Any) -> object:
        if crash_after_start:
            original_start(*args, **kwargs)
        raise RuntimeError("simulated process crash")

    monkeypatch.setattr(advance_queue_module, "start_published_run", crash)
    with pytest.raises(RuntimeError, match="simulated process crash"):
        first.launch()
    first.close()
    monkeypatch.setattr(advance_queue_module, "start_published_run", original_start)

    reopened = _runtime(database_path)
    try:
        reopened.launch()
        with reopened.engine.connect() as connection:
            runs = connection.execute(
                sa.text("SELECT run_id, revision_hash FROM runs")
            ).all()
            bindings = (
                connection.execute(
                    sa.select(queue_launch_bindings).where(
                        queue_launch_bindings.c.item_id == reference.item_id.value
                    )
                )
                .mappings()
                .all()
            )
        assert len(runs) == len(bindings) == 1
        assert runs[0].run_id == bindings[0]["run_id"]
        assert runs[0].revision_hash == bindings[0]["workflow_revision_hash"]
    finally:
        reopened.close()


def test_a_serve_launch_starts_an_admitted_item_in_a_policy_less_project(
    tmp_path: Path,
) -> None:
    """No published policy revision means no cap (ruling 28.08.2026): the serve
    starts the item on launch instead of raising `QueueAdvanceCorrupt`.
    """
    database_path = tmp_path / "atelier.sqlite"
    runtime = _runtime(database_path)
    runtime.initialize_storage()
    lineage_id, _revision_hash = _found_lineage(runtime.engine)
    queue = DbosQueueProjectionStore(runtime.engine)
    reference = _prepare_admitted(queue, lineage_id, policy_revision=None)

    try:
        runtime.launch()
        with runtime.engine.connect() as connection:
            bindings = (
                connection.execute(
                    sa.select(queue_launch_bindings).where(
                        queue_launch_bindings.c.item_id == reference.item_id.value
                    )
                )
                .mappings()
                .all()
            )
        assert len(bindings) == 1
    finally:
        runtime.close()


@pytest.mark.proves("the-automation-label-admits-the-items-that-carry-it")
def test_a_serve_launch_proposes_a_label_only_item_from_the_policy_defaults(
    tmp_path: Path,
) -> None:
    """The operator's whole handgrip is the label: policy defaults do the rest.

    The item enters the store as an import leaves it -- observed, with no
    proposal anyone wrote -- and the published policy names the workflow, the
    rank and the authorisation a labelled item is proposed under. One Serve
    launch therefore proposes it from those defaults, admits it under the
    automation rule, and starts the one launch it is bound to.
    """

    label = "bereit"
    project_root = tmp_path / "operator-project"
    project_root.mkdir()
    listing = OpenTrackerItemsObserved(
        (
            ObservedOpenTrackerItem(
                TrackerItemReference("gh:1236"), "labelled", (label,)
            ),
        ),
        RecordedAt("2026-09-05T09:00:00Z"),
    )
    runtime = _runtime(
        tmp_path / "atelier.sqlite",
        project_root=project_root,
        tracker=FakeTrackerItemSource(open_items_answer=listing),
    )
    try:
        lineage_id, _revision_hash = _found_lineage(runtime.engine)
        queue = DbosQueueProjectionStore(runtime.engine)
        assert isinstance(
            queue.put_policy(
                QueueProjectPolicyRevision(
                    PROJECT,
                    1,
                    1,
                    label,
                    QueueProjectPolicyDefaults(
                        lineage_id,
                        QueuePriorityRank(5),
                        QueueAutomationDisposition.AUTOMATION_AUTHORIZED,
                    ),
                ),
                0,
            ),
            QueueProjectPolicyPublished,
        )
        reference = WorkItemReference(PROJECT, TrackerItemReference("gh:1236"))
        _seed_open_items(queue, reference)

        runtime.launch()

        started = _snapshots_by_reference(queue)[reference.tracker_item]
        assert started.state is QueueItemState.ADMITTED
        assert started.proposal == QueueProposal(
            QueuePriorityRank(5),
            lineage_id,
            (),
            QueueAutomationDisposition.AUTOMATION_AUTHORIZED,
            1,
            QueueProposalSource.POLICY_DEFAULT,
        )
        assert started.admission is not None
        assert started.admission.authority is QueueDecisionAuthority.AUTOMATION_RULE
        assert len(_launch_bindings(runtime.engine, reference.item_id)) == 1
    finally:
        runtime.close()


@pytest.mark.proves("the-automation-label-admits-the-items-that-carry-it")
def test_a_serve_launch_admits_the_labelled_item_and_starts_it_in_the_same_sweep(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The label's whole way through a Serve start: policy, tracker, admission, start.

    The unit scenarios own what the rule decides per item; what this proves is
    the composition around it -- a policy published with a non-null label is
    the one the runtime reads back, the tracker answer is read at the sweep,
    and the item that label admits is started by the same sweep rather than by
    the next process start. The two items beside it are the boundary: one
    carries the label but is reserved for a human, one is automatable but
    carries no label, and neither is admitted.
    """

    label = "bereit"
    project_root = tmp_path / "operator-project"
    project_root.mkdir()
    listing = OpenTrackerItemsObserved(
        (
            ObservedOpenTrackerItem(
                TrackerItemReference("gh:79"), "labelled", (label,)
            ),
            ObservedOpenTrackerItem(
                TrackerItemReference("gh:80"), "reserved for a human", (label,)
            ),
            ObservedOpenTrackerItem(TrackerItemReference("gh:81"), "unlabelled", ()),
        ),
        RecordedAt("2026-09-04T09:00:00Z"),
    )
    runtime = _runtime(
        tmp_path / "atelier.sqlite",
        project_root=project_root,
        tracker=FakeTrackerItemSource(open_items_answer=listing),
    )
    try:
        lineage_id, _revision_hash = _found_lineage(runtime.engine)
        queue = DbosQueueProjectionStore(runtime.engine)
        assert isinstance(
            queue.put_policy(QueueProjectPolicyRevision(PROJECT, 1, 1, label), 0),
            QueueProjectPolicyPublished,
        )
        automatic = _prepare_proposed(
            queue,
            lineage_id,
            "gh:79",
            disposition=QueueAutomationDisposition.AUTOMATION_AUTHORIZED,
        ).item_reference
        _prepare_proposed(queue, lineage_id, "gh:80")
        _prepare_proposed(
            queue,
            lineage_id,
            "gh:81",
            disposition=QueueAutomationDisposition.AUTOMATION_AUTHORIZED,
        )

        with caplog.at_level(logging.INFO, logger="atelier2"):
            runtime.launch()

        snapshots = _snapshots_by_reference(queue)
        admitted = snapshots[TrackerItemReference("gh:79")]
        assert admitted.state is QueueItemState.ADMITTED
        assert admitted.admission is not None
        assert admitted.admission.authority is QueueDecisionAuthority.AUTOMATION_RULE
        assert label in admitted.admission.rationale.value
        assert snapshots[TrackerItemReference("gh:80")].state is QueueItemState.PROPOSED
        assert snapshots[TrackerItemReference("gh:81")].state is QueueItemState.PROPOSED
        assert len(_launch_bindings(runtime.engine, automatic.item_id)) == 1

        swept = [
            record
            for record in caplog.records
            if getattr(record, "event", None) == "queue_label_admission_swept"
        ]
        assert [
            (getattr(record, "admitted", None), getattr(record, "declined", None))
            for record in swept
        ] == [(1, 1)]
        assert [
            (getattr(record, "item_id", None), getattr(record, "outcome", None))
            for record in caplog.records
            if getattr(record, "event", None) == "queue_label_admission_declined"
        ] == [
            (
                WorkItemReference(PROJECT, TrackerItemReference("gh:80")).item_id.value,
                QueueAdmissionAuthorityRefused.__name__,
            )
        ]
    finally:
        runtime.close()


def test_a_serve_launch_admits_nothing_and_warns_when_the_tracker_cannot_be_read(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """An unreachable tracker leaves every row as it was -- and says so.

    The rule is soft by contract: nothing durable changes, so the sweep's own
    line is the only thing an operator can see it by.
    """

    project_root = tmp_path / "operator-project"
    project_root.mkdir()
    unreadable = TrackerSourceUnavailable("the tracker refused the listing")
    runtime = _runtime(
        tmp_path / "atelier.sqlite",
        project_root=project_root,
        tracker=FakeTrackerItemSource(open_items_answer=unreadable),
    )
    try:
        lineage_id, _revision_hash = _found_lineage(runtime.engine)
        queue = DbosQueueProjectionStore(runtime.engine)
        queue.put_policy(QueueProjectPolicyRevision(PROJECT, 1, 1, "bereit"), 0)
        proposed = _prepare_proposed(
            queue,
            lineage_id,
            disposition=QueueAutomationDisposition.AUTOMATION_AUTHORIZED,
        ).item_reference

        with caplog.at_level(logging.INFO, logger="atelier2"):
            runtime.launch()

        snapshots = _snapshots_by_reference(queue)
        assert snapshots[proposed.tracker_item].state is QueueItemState.PROPOSED
        assert _launch_bindings(runtime.engine, proposed.item_id) == ()
        assert [
            (record.levelno, getattr(record, "detail", None))
            for record in caplog.records
            if getattr(record, "event", None)
            == "queue_label_admission_source_unreadable"
        ] == [(logging.WARNING, unreadable.detail)]
    finally:
        runtime.close()


def test_the_newest_policy_revision_answers_with_the_automation_label_it_published(
    store: tuple[DbosQueueProjectionStore, Engine],
) -> None:
    """Both rules of a policy survive the store, and the newest revision rules.

    A label the store cannot read back is a label the sweep cannot act on, so
    the round trip is part of the contract, not of the adapter's internals.
    """

    queue, _engine = store
    assert isinstance(
        queue.put_policy(QueueProjectPolicyRevision(PROJECT, 1, 1, "bereit"), 0),
        QueueProjectPolicyPublished,
    )
    assert isinstance(
        queue.put_policy(QueueProjectPolicyRevision(PROJECT, 2, 3, "startklar"), 1),
        QueueProjectPolicyPublished,
    )

    current = queue.current_policy(PROJECT)

    assert current == QueueProjectPolicyFound(
        QueueProjectPolicyRevision(PROJECT, 2, 3, "startklar")
    )


def test_advance_replays_a_reserved_binding_before_projection_blockers(
    store: tuple[DbosQueueProjectionStore, Engine],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queue, engine = store
    lineage_id, revision_hash = _found_lineage(engine)
    queue.put_policy(QueueProjectPolicyRevision(PROJECT, 1, 1, None), 0)
    reference = _prepare_admitted(queue, lineage_id)
    binding = QueueLaunchBinding(
        reference.item_id,
        QueueProjectionRevision(1),
        RunId("reserved-replay"),
        revision_hash,
    )
    assert isinstance(queue.reserve_launch(binding), QueueLaunchReserved)
    page = queue.list_items(None, 50)
    assert isinstance(page, QueueItemsPage)
    (stored,) = page.items
    assert stored.blockers == ()

    class BoundQueue:
        def list_items(self, _after: object, _limit: int) -> QueueItemsPage:
            return QueueItemsPage(
                (replace(stored, blockers=(QueueBlockerKind.CAP_REACHED,)),), None
            )

        def reserve_launch(self, _binding: object) -> object:
            raise AssertionError("a stored binding must not be reserved again")

        def read_launch(self, _binding: object) -> QueueLaunchRunOpen:
            return QueueLaunchRunOpen()

    def started(
        run_id: RunId,
        workflow_revision_hash: WorkflowRevisionHash,
        _bindings: object,
        _starter: object,
        **_kwargs: object,
    ) -> RunCreated:
        return RunCreated(
            Run(run_id, workflow_revision_hash, RunState.STARTED, "final", 0, 0)
        )

    monkeypatch.setattr(advance_queue_module, "start_published_run", started)

    outcomes = advance_queue_module.advance_queue(
        cast(Any, BoundQueue()),
        DbosCatalogStore(engine),
        cast(DurablePublishedRunStarter, object()),
        workflow_document_parser=parse_workflow_document,
    )

    assert isinstance(outcomes[0], QueueRunStarted)
    assert outcomes[0].binding == binding


@pytest.mark.parametrize(
    ("read_answer", "expected_error"),
    [
        (QueueReadUnavailable(), QueueAdvanceUnavailable),
        (DurableStateCorrupt(), QueueAdvanceCorrupt),
        (object(), QueueAdvanceCorrupt),
    ],
)
def test_advance_classifies_queue_read_failures(
    read_answer: object,
    expected_error: type[RuntimeError],
) -> None:
    class ReadAnswerQueue:
        def list_items(self, _after: object, _limit: int) -> object:
            return read_answer

    with pytest.raises(expected_error):
        advance_queue_module.advance_queue(
            cast(Any, ReadAnswerQueue()),
            cast(Any, object()),
            cast(DurablePublishedRunStarter, object()),
            workflow_document_parser=parse_workflow_document,
        )


@pytest.mark.parametrize(
    "corruption",
    ["proposed-without-proposal", "authority-without-proposal-revision"],
)
def test_advance_refuses_incomplete_phase_d_projection_before_blockers(
    store: tuple[DbosQueueProjectionStore, Engine],
    corruption: str,
) -> None:
    queue, engine = store
    lineage_id, _revision_hash = _found_lineage(engine)
    queue.put_policy(QueueProjectPolicyRevision(PROJECT, 1, 1, None), 0)
    reference = _prepare_admitted(queue, lineage_id, f"gh:{corruption}")
    page = queue.list_items(None, 50)
    assert isinstance(page, QueueItemsPage)
    stored = next(
        item for item in page.items if item.item_reference.item_id == reference.item_id
    )
    assert stored.admission is not None
    if corruption == "proposed-without-proposal":
        malformed = SimpleNamespace(
            item_reference=stored.item_reference,
            state=QueueItemState.PROPOSED,
            revision=QueueProjectionRevision(1),
            admission=None,
            proposal=None,
            launch_binding=None,
            blockers=(QueueBlockerKind.HUMAN_REQUIRED,),
        )
    else:
        malformed = SimpleNamespace(
            item_reference=stored.item_reference,
            state=stored.state,
            revision=stored.revision,
            admission=SimpleNamespace(
                workflow_lineage_id=lineage_id,
                rationale=stored.admission.rationale,
                authority=QueueDecisionAuthority.OPERATOR,
                proposal_revision=None,
            ),
            proposal=stored.proposal,
            launch_binding=None,
            blockers=(QueueBlockerKind.CAP_REACHED,),
        )

    class MalformedProjection:
        def list_items(self, _after: object, _limit: int) -> QueueItemsPage:
            return QueueItemsPage((cast(QueueItemSnapshot, malformed),), None)

        def reserve_launch(self, _binding: object) -> object:
            raise AssertionError("corrupt projection must fail before reservation")

    with pytest.raises(QueueAdvanceCorrupt, match="inconsistent item"):
        advance_queue_module.advance_queue(
            cast(Any, MalformedProjection()),
            DbosCatalogStore(engine),
            cast(DurablePublishedRunStarter, object()),
            workflow_document_parser=parse_workflow_document,
        )


@pytest.mark.parametrize(
    ("reservation_answer", "expected_error"),
    [
        (DurableWriteUnavailable(), QueueAdvanceUnavailable),
        (DurableStateCorrupt(), QueueAdvanceCorrupt),
        (object(), QueueAdvanceCorrupt),
    ],
)
def test_advance_classifies_launch_reservation_failures(
    store: tuple[DbosQueueProjectionStore, Engine],
    reservation_answer: object,
    expected_error: type[RuntimeError],
) -> None:
    queue, engine = store
    lineage_id, _revision_hash = _found_lineage(engine)
    queue.put_policy(QueueProjectPolicyRevision(PROJECT, 1, 1, None), 0)
    _prepare_admitted(queue, lineage_id)
    page = queue.list_items(None, 50)
    assert isinstance(page, QueueItemsPage)

    class ReservationAnswerQueue:
        def list_items(self, _after: object, _limit: int) -> QueueItemsPage:
            return page

        def reserve_launch(self, _binding: object) -> object:
            return reservation_answer

    with pytest.raises(expected_error):
        advance_queue_module.advance_queue(
            cast(Any, ReservationAnswerQueue()),
            DbosCatalogStore(engine),
            cast(DurablePublishedRunStarter, object()),
            workflow_document_parser=parse_workflow_document,
        )


@pytest.mark.parametrize(
    ("catalog_answer", "expected_error"),
    [
        (PublishedRevisionsUnavailable(), QueueAdvanceUnavailable),
        (DurableStateCorrupt(), QueueAdvanceCorrupt),
        (object(), QueueAdvanceCorrupt),
    ],
)
def test_advance_classifies_catalog_resolution_failures(
    store: tuple[DbosQueueProjectionStore, Engine],
    catalog_answer: object,
    expected_error: type[RuntimeError],
) -> None:
    queue, engine = store
    lineage_id, _revision_hash = _found_lineage(engine)
    queue.put_policy(QueueProjectPolicyRevision(PROJECT, 1, 1, None), 0)
    _prepare_admitted(queue, lineage_id)

    class CatalogAnswer:
        def resolve_name(self, *_args: object) -> object:
            return catalog_answer

    with pytest.raises(expected_error):
        advance_queue_module.advance_queue(
            queue,
            cast(Any, CatalogAnswer()),
            cast(DurablePublishedRunStarter, object()),
            workflow_document_parser=parse_workflow_document,
        )


@pytest.mark.parametrize(
    ("start_failure", "expected_error"),
    [
        (WriteUnavailable(), QueueAdvanceUnavailable),
        (ApplicationDurableStateCorrupt(), QueueAdvanceCorrupt),
        (object(), QueueAdvanceCorrupt),
    ],
)
def test_advance_classifies_reserved_run_start_failures(
    store: tuple[DbosQueueProjectionStore, Engine],
    monkeypatch: pytest.MonkeyPatch,
    start_failure: object,
    expected_error: type[RuntimeError],
) -> None:
    queue, engine = store
    lineage_id, revision_hash = _found_lineage(engine)
    queue.put_policy(QueueProjectPolicyRevision(PROJECT, 1, 1, None), 0)
    reference = _prepare_admitted(queue, lineage_id)
    assert isinstance(
        queue.reserve_launch(
            QueueLaunchBinding(
                reference.item_id,
                QueueProjectionRevision(1),
                RunId("reserved-start-failure"),
                revision_hash,
            )
        ),
        QueueLaunchReserved,
    )
    monkeypatch.setattr(
        advance_queue_module,
        "start_published_run",
        lambda *_args, **_kwargs: start_failure,
    )

    with pytest.raises(expected_error):
        advance_queue_module.advance_queue(
            queue,
            DbosCatalogStore(engine),
            cast(DurablePublishedRunStarter, object()),
            workflow_document_parser=parse_workflow_document,
        )


@pytest.mark.proves("a-manually-approved-queue-item-starts-once")
def test_a_moved_lineage_head_does_not_launch_the_item_again(tmp_path: Path) -> None:
    database_path = tmp_path / "atelier.sqlite"
    first = _runtime(database_path)
    lineage_id, original_revision = _found_lineage(first.engine)
    queue = DbosQueueProjectionStore(first.engine)
    queue.put_policy(QueueProjectPolicyRevision(PROJECT, 1, 1, None), 0)
    _prepare_admitted(queue, lineage_id)
    first.launch()
    first.close()

    second = _runtime(database_path)
    changed = BINDING_FREE_WORKFLOW.replace(b"[2, 3]", b"[3, 4]")
    revision = WorkflowRevision(changed)
    publish_revision(second.engine, revision)
    catalog = DbosCatalogStore(second.engine)
    published = PublishedRevision(RevisionKind.WORKFLOW, changed)
    catalog.publish_revision(published)
    admitted = catalog.admit_member(
        lineage_id,
        published,
        CatalogLineageDisplayName(f"phase-d-{original_revision.value[:8]}"),
        CatalogActor("operator"),
        CatalogActivatedAt("2026-08-27T11:00:00Z"),
    )
    assert admitted is not None
    try:
        second.launch()
        with second.engine.connect() as connection:
            assert connection.scalar(sa.text("SELECT COUNT(*) FROM runs")) == 1
            assert (
                connection.scalar(
                    sa.select(queue_launch_bindings.c.workflow_revision_hash)
                )
                == original_revision.value
            )
    finally:
        second.close()


def test_queue_api_exposes_one_typed_projection_and_confirmation_matrix(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path / "atelier.sqlite")
    runtime.initialize_storage()
    api: TestClient = durable_api_client(runtime)
    lineage_id, _revision_hash = _found_lineage(runtime.engine)
    public_project = encode_public_project_reference(PROJECT)
    policy_path = PROJECT_QUEUE_POLICY_PATH.replace(
        "{public_project_reference}", public_project
    )
    try:
        policy = api.put(
            policy_path,
            json={
                "revision_number": 1,
                "expected_revision": 0,
                "maximum_active_runs": 1,
                "automation_label": None,
                "default_workflow_lineage_id": lineage_id.value,
                "default_priority_rank": 4,
            },
        )
        assert policy.status_code == 201, policy.text
        assert policy.json()["default_workflow_lineage_id"] == lineage_id.value
        assert policy.json()["default_priority_rank"] == 4
        assert policy.json()["automation_disposition_default"] == "HUMAN_REQUIRED"
        proposal = api.put(
            QUEUE_PROPOSALS_PATH,
            json={
                "project_id": PROJECT.value,
                "tracker_item_reference": "gh:79",
                "expected_revision": 0,
                "priority": {"rank": 1},
                "workflow_lineage_id": lineage_id.value,
                "prerequisite_item_ids": [],
                "automation_disposition": "HUMAN_REQUIRED",
                "policy_revision": 1,
            },
        )
        assert proposal.status_code == 201, proposal.text
        stale = api.post(
            QUEUE_ADMISSIONS_PATH,
            json={
                "project_id": PROJECT.value,
                "tracker_item_reference": "gh:79",
                "expected_revision": 0,
                "rationale": "approved",
            },
        )
        assert stale.status_code == 409
        admitted = api.post(
            QUEUE_ADMISSIONS_PATH,
            json={
                "project_id": PROJECT.value,
                "tracker_item_reference": "gh:79",
                "expected_revision": 1,
                "rationale": "approved",
            },
        )
        assert admitted.status_code == 201, admitted.text
        assert admitted.json()["admission"]["authority"] == "OPERATOR"
        projection = api.get(QUEUE_ITEMS_PATH)
        assert projection.status_code == 200, projection.text
        (row,) = projection.json()["items"]
        assert row["state"] == "ADMITTED"
        assert row["proposal"]["priority"] == {"rank": 1}
        assert row["proposal"]["source"] == "OPERATOR"
        assert row["tracker_enrichment"] == "ENRICHMENT_UNAVAILABLE"
        assert row["title"] is None
        assert api.get(API_PREFIX + "/observed-queue-items").status_code == 404
    finally:
        runtime.close()


def test_a_queue_policy_default_names_a_workflow_and_a_priority_together(
    store: tuple[DbosQueueProjectionStore, Engine],
) -> None:
    """Half a default is refused at the door instead of guessing the other half."""

    queue, _engine = store
    policy_path = PROJECT_QUEUE_POLICY_PATH.replace(
        "{public_project_reference}", encode_public_project_reference(PROJECT)
    )

    with _queue_api(queue) as api:
        response = api.put(
            policy_path,
            json={
                "revision_number": 1,
                "expected_revision": 0,
                "maximum_active_runs": 1,
                "automation_label": "bereit",
                "default_priority_rank": 4,
            },
        )

    assert response.status_code == 422
    assert response.json()["type"].endswith("invalid-request")
    assert isinstance(queue.current_policy(PROJECT), QueueProjectPolicyAbsent)


def _queue_api(
    queue: object, request_queue_sweep: Callable[[], None] | None = None
) -> TestClient:
    return TestClient(
        create_app(
            source_commit="commit",
            source_tree="tree",
            ports=api_ports(
                queue_projection=queue, request_queue_sweep=request_queue_sweep
            ),
            limits=api_limits(),
            event_poll_backoff=event_poll_backoff(),
        )
    )


def test_the_admission_door_asks_for_a_sweep_of_its_own_admission(
    store: tuple[DbosQueueProjectionStore, Engine],
) -> None:
    """An admitted item does not wait out the sweep's tick to start."""

    queue, engine = store
    lineage_id, _revision_hash = _found_lineage(engine)
    queue.put_policy(QueueProjectPolicyRevision(PROJECT, 1, 1, None), 0)
    proposed = _prepare_proposed(queue, lineage_id, "gh:admitted-then-swept")
    asked: list[None] = []

    with _queue_api(queue, lambda: asked.append(None)) as api:
        response = api.post(
            QUEUE_ADMISSIONS_PATH,
            json={
                "project_id": PROJECT.value,
                "tracker_item_reference": (proposed.item_reference.tracker_item.value),
                "expected_revision": proposed.revision.value,
                "rationale": "operator approved the inspected proposal",
            },
        )

    assert response.status_code == 201
    assert len(asked) == 1


def test_a_refused_admission_asks_for_no_sweep(
    store: tuple[DbosQueueProjectionStore, Engine],
) -> None:
    """Nothing was admitted, so there is nothing for a sweep to start."""

    queue, _engine = store
    reference = WorkItemReference(PROJECT, TrackerItemReference("gh:unproposed-sweep"))
    _seed_open_items(queue, reference)
    asked: list[None] = []

    with _queue_api(queue, lambda: asked.append(None)) as api:
        response = api.post(
            QUEUE_ADMISSIONS_PATH,
            json={
                "project_id": PROJECT.value,
                "tracker_item_reference": reference.tracker_item.value,
                "expected_revision": 0,
                "rationale": "cannot skip proposal",
            },
        )

    assert response.status_code == 409
    assert asked == []


@pytest.mark.parametrize(
    "corruption",
    [
        "observed-revision",
        "observed-admission",
        "proposed-revision",
        "proposed-mismatched-revisions",
        "proposed-admission",
        "admitted-null-rationale",
    ],
)
def test_queue_api_fails_loud_for_illegal_raw_lifecycle_shapes(
    store: tuple[DbosQueueProjectionStore, Engine],
    corruption: str,
) -> None:
    queue, engine = store
    lineage_id, _revision_hash = _found_lineage(engine)
    queue.put_policy(QueueProjectPolicyRevision(PROJECT, 1, 1, None), 0)
    reference = WorkItemReference(PROJECT, TrackerItemReference(f"gh:{corruption}"))
    if corruption.startswith("admitted"):
        _prepare_admitted(queue, lineage_id, reference.tracker_item.value)
    else:
        _seed_open_items(queue, reference)
        if corruption.startswith("proposed"):
            assert isinstance(
                queue.plan(
                    PlanQueueItem(
                        reference,
                        _proposal(lineage_id),
                        QueueProjectionRevision(0),
                    )
                ),
                QueueItemProposed,
            )
    corrupt_values_by_case: dict[str, dict[str, Any]] = {
        "observed-revision": {"state_version": 1},
        "observed-admission": {"admission_rationale": "ghost admission"},
        "proposed-revision": {"state_version": 0},
        "proposed-mismatched-revisions": {"state_version": 2},
        "proposed-admission": {"workflow_lineage_id": lineage_id.value},
        "admitted-null-rationale": {"admission_rationale": None},
    }
    corrupt_values = corrupt_values_by_case[corruption]
    with engine.begin() as connection:
        connection.exec_driver_sql("DROP TRIGGER queue_items_state_transition")
        connection.exec_driver_sql("PRAGMA ignore_check_constraints=ON")
        connection.execute(
            schema_module.queue_items.update()
            .where(schema_module.queue_items.c.item_id == reference.item_id.value)
            .values(**corrupt_values)
        )

    with _queue_api(queue) as api:
        response = api.get(QUEUE_ITEMS_PATH)

    assert response.status_code == 500
    assert response.json()["type"].endswith("durable-state-corrupt")


def test_queue_admission_authority_refusal_has_its_own_problem() -> None:
    class AuthorityRefusingQueue:
        def confirm(self, _command: object) -> QueueAdmissionAuthorityRefused:
            return QueueAdmissionAuthorityRefused(
                QueueDecisionAuthority.AUTOMATION_RULE,
                QueueAutomationDisposition.HUMAN_REQUIRED,
            )

    with _queue_api(AuthorityRefusingQueue()) as api:
        response = api.post(
            QUEUE_ADMISSIONS_PATH,
            json={
                "project_id": PROJECT.value,
                "tracker_item_reference": "gh:79",
                "expected_revision": 1,
                "rationale": "automation tried",
            },
        )

    assert response.status_code == 409
    assert response.json()["type"].endswith("queue-admission-authority-refused")


def test_queue_admission_api_requires_a_proposal_before_confirmation(
    store: tuple[DbosQueueProjectionStore, Engine],
) -> None:
    queue, _engine = store
    reference = WorkItemReference(PROJECT, TrackerItemReference("gh:unproposed"))
    _seed_open_items(queue, reference)

    with _queue_api(queue) as api:
        response = api.post(
            QUEUE_ADMISSIONS_PATH,
            json={
                "project_id": PROJECT.value,
                "tracker_item_reference": reference.tracker_item.value,
                "expected_revision": 0,
                "rationale": "cannot skip proposal",
            },
        )

    assert response.status_code == 409
    assert response.json()["type"].endswith("queue-admission-proposal-required")


def test_corrupt_admission_proposal_identity_fails_projection_api_and_start(
    store: tuple[DbosQueueProjectionStore, Engine],
) -> None:
    queue, engine = store
    lineage_id, revision_hash = _found_lineage(engine)
    other_lineage_id, _other_revision = _found_lineage(
        engine, BINDING_FREE_WORKFLOW.replace(b"[2, 3]", b"[4, 5]")
    )
    queue.put_policy(QueueProjectPolicyRevision(PROJECT, 1, 1, None), 0)
    reference = _prepare_admitted(queue, lineage_id, "gh:corrupt-admission")
    with engine.begin() as connection:
        connection.exec_driver_sql("DROP TRIGGER queue_items_state_transition")
        connection.execute(
            schema_module.queue_items.update()
            .where(schema_module.queue_items.c.item_id == reference.item_id.value)
            .values(workflow_lineage_id=other_lineage_id.value)
        )

    reservation = queue.reserve_launch(
        QueueLaunchBinding(
            reference.item_id,
            QueueProjectionRevision(1),
            RunId("corrupt-admission-reservation"),
            revision_hash,
        )
    )
    assert isinstance(reservation, DurableStateCorrupt)
    with engine.connect() as connection:
        assert (
            connection.scalar(
                sa.select(sa.func.count()).select_from(queue_launch_bindings)
            )
            == 0
        )
    assert isinstance(queue.list_items(None, 50), DurableStateCorrupt)
    with _queue_api(queue) as api:
        response = api.get(QUEUE_ITEMS_PATH)
    assert response.status_code == 500
    assert response.json()["type"].endswith("durable-state-corrupt")
    with pytest.raises(QueueAdvanceCorrupt, match="queue is corrupt"):
        advance_queue_module.advance_queue(
            queue,
            DbosCatalogStore(engine),
            cast(DurablePublishedRunStarter, object()),
            workflow_document_parser=parse_workflow_document,
        )


def _ended_run(
    engine: Engine,
    run_id: RunId,
    revision_hash: WorkflowRevisionHash,
    state: RunState,
) -> None:
    """One durable run row already at its ending, for the sweep to read."""

    with engine.begin() as connection:
        connection.execute(
            sa.insert(schema_module.runs).values(
                run_id=run_id.value,
                bootstrap_workflow_id=f"bootstrap-{run_id.value}",
                revision_hash=revision_hash.value,
                workflow_format_version=int(WorkflowFormatVersion.V1),
                current_node_id="final",
                current_round_ordinal=1,
                state=state.value,
                state_version=1,
                last_event_sequence=1,
                terminal_hash="e" * 64,
            )
        )


def _bound_and_ended(
    queue: DbosQueueProjectionStore,
    engine: Engine,
    reference: WorkItemReference,
    revision_hash: WorkflowRevisionHash,
    run_id: RunId,
    state: RunState,
    proposal_revision: int = 1,
) -> QueueLaunchBinding:
    """Reserve this item's launch and leave its run at the named ending."""

    binding = QueueLaunchBinding(
        reference.item_id,
        QueueProjectionRevision(proposal_revision),
        run_id,
        revision_hash,
    )
    assert isinstance(queue.reserve_launch(binding), QueueLaunchReserved)
    _ended_run(engine, run_id, revision_hash, state)
    return binding


def _stored_bindings(engine: Engine) -> list[tuple[object, ...]]:
    with engine.connect() as connection:
        return [
            tuple(record)
            for record in connection.execute(
                sa.select(
                    queue_launch_bindings.c.run_id,
                    queue_launch_bindings.c.ended_run_state,
                    queue_launch_bindings.c.restart_ordinal,
                ).order_by(queue_launch_bindings.c.proposal_revision)
            )
        ]


def test_a_failed_launch_is_released_onto_the_next_proposal_it_carries_forward(
    store: tuple[DbosQueueProjectionStore, Engine],
) -> None:
    """The item comes back admitted, prerequisites and all, one revision on.

    The proposal is re-issued rather than re-decided: same workflow, same rank,
    same prerequisites. Only the revision moves, which is what makes the next
    launch a differently identified run instead of a second attempt at the one
    that failed.
    """

    queue, engine = store
    lineage_id, revision_hash = _found_lineage(engine)
    queue.put_policy(QueueProjectPolicyRevision(PROJECT, 1, 5, None), 0)
    prerequisite = _prepare_admitted(queue, lineage_id, "gh:pre")
    _bound_and_ended(
        queue,
        engine,
        prerequisite,
        revision_hash,
        RunId("prerequisite-run"),
        RunState.COMPLETED,
    )
    reference = _prepare_admitted(
        queue, lineage_id, "gh:79", prerequisites=(prerequisite.item_id,)
    )
    binding = _bound_and_ended(
        queue, engine, reference, revision_hash, RunId("failed-run"), RunState.FAILED
    )

    assert queue.read_launch(binding) == QueueLaunchRunEnded(RunState.FAILED, 0)
    released = queue.release_launch(ReleaseQueueLaunch(binding, RunState.FAILED))

    assert isinstance(released, QueueLaunchReleased)
    page = queue.list_items(None, 50)
    assert isinstance(page, QueueItemsPage)
    (item,) = [
        candidate for candidate in page.items if candidate.item_reference == reference
    ]
    assert item.state is QueueItemState.ADMITTED
    assert item.admission is not None
    assert item.admission.proposal_revision == QueueProjectionRevision(2)
    assert item.launch_binding is None
    assert item.blockers == ()
    assert item.proposal is not None
    assert item.proposal.prerequisite_item_ids == (prerequisite.item_id,)

    restarted = QueueLaunchBinding(
        reference.item_id,
        QueueProjectionRevision(2),
        RunId("second-run"),
        revision_hash,
    )
    assert isinstance(queue.reserve_launch(restarted), QueueLaunchReserved)
    assert _stored_bindings(engine) == [
        ("prerequisite-run", None, 0),
        ("failed-run", RunState.FAILED.value, 0),
        ("second-run", None, 1),
    ]


def test_two_sweeps_release_the_same_ended_launch_exactly_once(
    store: tuple[DbosQueueProjectionStore, Engine],
) -> None:
    """The binding's own ending is the compare-and-set, so one sweep wins it.

    Without that, both sweeps would advance the item's revision and the second
    would start a run nobody decided to pay for.
    """

    queue, engine = store
    lineage_id, revision_hash = _found_lineage(engine)
    queue.put_policy(QueueProjectPolicyRevision(PROJECT, 1, 5, None), 0)
    reference = _prepare_admitted(queue, lineage_id)
    binding = _bound_and_ended(
        queue,
        engine,
        reference,
        revision_hash,
        RunId("cancelled-run"),
        RunState.CANCELLED,
    )
    barrier = Barrier(2)

    def release(_index: int) -> object:
        barrier.wait()
        return queue.release_launch(ReleaseQueueLaunch(binding, RunState.CANCELLED))

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = tuple(executor.map(release, range(2)))

    assert sum(isinstance(outcome, QueueLaunchReleased) for outcome in outcomes) == 1
    assert (
        sum(isinstance(outcome, QueueLaunchReleaseRefused) for outcome in outcomes) == 1
    )
    page = queue.list_items(None, 50)
    assert isinstance(page, QueueItemsPage)
    (item,) = page.items
    assert item.admission is not None
    assert item.admission.proposal_revision == QueueProjectionRevision(2)
    assert item.revision == QueueProjectionRevision(3)


def test_a_store_that_refuses_the_items_advance_releases_nothing(
    store: tuple[DbosQueueProjectionStore, Engine],
) -> None:
    """A half-released item would be bound to nothing and startable twice."""

    queue, engine = store
    lineage_id, revision_hash = _found_lineage(engine)
    queue.put_policy(QueueProjectPolicyRevision(PROJECT, 1, 5, None), 0)
    reference = _prepare_admitted(queue, lineage_id)
    binding = _bound_and_ended(
        queue, engine, reference, revision_hash, RunId("failed-run"), RunState.FAILED
    )

    def refuse_the_advance(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        if statement.startswith("UPDATE queue_items"):
            raise OperationalError("advance", {}, Exception("the disk is full"))

    event.listen(engine, "before_cursor_execute", refuse_the_advance)
    try:
        refused = queue.release_launch(ReleaseQueueLaunch(binding, RunState.FAILED))
    finally:
        event.remove(engine, "before_cursor_execute", refuse_the_advance)

    assert isinstance(refused, DurableWriteUnavailable)
    assert _stored_bindings(engine) == [("failed-run", None, 0)]
    page = queue.list_items(None, 50)
    assert isinstance(page, QueueItemsPage)
    (item,) = page.items
    assert item.admission is not None
    assert item.admission.proposal_revision == QueueProjectionRevision(1)
    assert item.launch_binding == binding
    with engine.connect() as connection:
        assert (
            connection.scalar(
                sa.select(sa.func.count()).select_from(
                    schema_module.queue_proposal_revisions
                )
            )
            == 1
        )


def test_the_release_itself_refuses_a_restart_past_the_cap(
    store: tuple[DbosQueueProjectionStore, Engine],
) -> None:
    """No writer of the port can buy a paid run the cap forbids.

    The sweep's own early answer is not in the loop here: the store is asked to
    release directly, once per allowed restart and once more, and the last ask
    leaves every row exactly as the previous release left it.
    """

    queue, engine = store
    lineage_id, revision_hash = _found_lineage(engine)
    queue.put_policy(QueueProjectPolicyRevision(PROJECT, 1, 5, None), 0)
    reference = _prepare_admitted(queue, lineage_id)
    for restart in range(MAXIMUM_QUEUE_LAUNCH_RESTARTS):
        binding = _bound_and_ended(
            queue,
            engine,
            reference,
            revision_hash,
            RunId(f"failed-run-{restart}"),
            RunState.FAILED,
            proposal_revision=restart + 1,
        )
        released = queue.release_launch(ReleaseQueueLaunch(binding, RunState.FAILED))
        assert isinstance(released, QueueLaunchReleased)
    last = _bound_and_ended(
        queue,
        engine,
        reference,
        revision_hash,
        RunId("failed-run-last"),
        RunState.FAILED,
        proposal_revision=MAXIMUM_QUEUE_LAUNCH_RESTARTS + 1,
    )
    before = _stored_bindings(engine)

    refused = queue.release_launch(ReleaseQueueLaunch(last, RunState.FAILED))

    assert refused == QueueLaunchRestartsExhausted(MAXIMUM_QUEUE_LAUNCH_RESTARTS)
    assert _stored_bindings(engine) == before
    assert before[-1] == ("failed-run-last", None, MAXIMUM_QUEUE_LAUNCH_RESTARTS)
    page = queue.list_items(None, 50)
    assert isinstance(page, QueueItemsPage)
    (item,) = page.items
    assert item.launch_binding == last


def _tracker_showing(*open_items: tuple[str, tuple[str, ...]]) -> FakeTrackerItemSource:
    return FakeTrackerItemSource(
        open_items_answer=OpenTrackerItemsObserved(
            tuple(
                ObservedOpenTrackerItem(TrackerItemReference(reference), "open", labels)
                for reference, labels in open_items
            ),
            THIRD_READ,
        )
    )


@pytest.mark.parametrize(
    ("tracker", "refusal"),
    [
        pytest.param(
            _tracker_showing(("gh:79", ("bereit",))), None, id="open, labelled"
        ),
        pytest.param(
            _tracker_showing(),
            QueueRestartRefusal.TRACKER_ITEM_CLOSED,
            id="tracker item closed",
        ),
        pytest.param(
            _tracker_showing(("gh:79", ())),
            QueueRestartRefusal.LABEL_REMOVED,
            id="label removed",
        ),
        pytest.param(
            FakeTrackerItemSource(
                open_items_answer=TrackerSourceUnavailable("the tracker is away")
            ),
            QueueRestartRefusal.TRACKER_UNREADABLE,
            id="tracker unreadable",
        ),
    ],
)
def test_two_concurrent_sweeps_restart_only_what_the_tracker_still_authorizes(
    store: tuple[DbosQueueProjectionStore, Engine],
    monkeypatch: pytest.MonkeyPatch,
    tracker: FakeTrackerItemSource,
    refusal: QueueRestartRefusal | None,
) -> None:
    """The tracker decides before the release, against the real rows.

    An open, labelled item's ended launch is released exactly once across the
    two sweeps; a closed or unlabelled item, and one the tracker could not be
    asked about, are released by neither: the binding row keeps its NULL
    ending, the item keeps its revision and its binding, and both sweeps name
    the same refusal.
    """

    queue, engine = store
    lineage_id, revision_hash = _found_lineage(engine)
    queue.put_policy(QueueProjectPolicyRevision(PROJECT, 1, 5, "bereit"), 0)
    reference = _prepare_admitted(queue, lineage_id)
    binding = _bound_and_ended(
        queue, engine, reference, revision_hash, RunId("failed-run"), RunState.FAILED
    )
    monkeypatch.setattr(advance_queue_module, "start_published_run", _run_started)
    barrier = Barrier(2)

    def sweep(_index: int) -> tuple[QueueAdvanceOutcome, ...]:
        barrier.wait()
        return advance_queue_module.advance_queue(
            queue,
            DbosCatalogStore(engine),
            cast(DurablePublishedRunStarter, object()),
            workflow_document_parser=parse_workflow_document,
            served_project=PROJECT,
            tracker=tracker,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = [
            outcome
            for sweep_outcomes in executor.map(sweep, range(2))
            for outcome in sweep_outcomes
        ]

    restarting = [
        outcome for outcome in outcomes if isinstance(outcome, QueueItemRestarting)
    ]
    withheld = [
        outcome for outcome in outcomes if isinstance(outcome, QueueItemRestartWithheld)
    ]
    (item,) = _snapshots_by_reference(queue).values()
    assert item.state is QueueItemState.ADMITTED
    assert item.retired_at is None
    assert item.admission is not None
    if refusal is None:
        assert len(restarting) == 1
        assert withheld == []
        assert _stored_bindings(engine) == [("failed-run", RunState.FAILED.value, 0)]
        assert item.admission.proposal_revision == QueueProjectionRevision(2)
        assert item.launch_binding is None
    else:
        assert restarting == []
        assert [outcome.refusal for outcome in withheld] == [refusal, refusal]
        assert _stored_bindings(engine) == [("failed-run", None, 0)]
        assert item.admission.proposal_revision == QueueProjectionRevision(1)
        assert item.launch_binding == binding
