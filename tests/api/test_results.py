from __future__ import annotations

from dataclasses import dataclass
from typing import cast

import pytest

from atelier2.adapters.yaml_workflows import parse_workflow_document
from atelier2.application.answer_wait import (
    AnswerAcceptedPending,
    AnswerActorMismatch,
    AnswerExistingApplied,
    AnswerExistingPending,
    AnswerRevisionConflict,
    AnswerStale,
    AnswerStateConflict,
    DurableStateCorrupt,
    NodeMissing,
    RunMissing,
    WriteUnavailable,
    answer_wait_result,
)
from atelier2.application.publish_workflow_revision import (
    PublicationCollision,
    PublicationCreated,
    PublicationExisting,
    PublicationInvalid,
    WorkflowPublicationLimits,
    publish_workflow_revision,
)
from atelier2.application.reconcile_effect import (
    ReconciliationAcceptedPending,
    ReconciliationCommandConflict,
    ReconciliationDeterminationConflict,
    ReconciliationExistingApplied,
    ReconciliationExistingPending,
    ReconciliationExistingRejected,
    ReconciliationStale,
    ReconciliationTargetMissing,
    reconcile_effect_result,
)
from atelier2.application.start_published_run import (
    AgentExecutorBindingUnavailable,
    RevisionMissing,
    RunCreated,
    RunExisting,
    RunFormatNotExecutable,
    RunIdentityConflict,
    start_published_run,
)
from atelier2.contracts.effects import (
    ReconcileCommand,
    ReconcileCommandSnapshot,
    ReconcileCommandState,
)
from atelier2.contracts.executions import (
    NodeExecutionId,
    WaitAnswer,
    WaitAnswerActor,
    WaitAnswerSnapshot,
    WaitAnswerState,
)
from atelier2.contracts.runs import Run, RunId, WorkflowRevision, WorkflowRevisionHash
from atelier2.contracts.workflow_refusals import (
    WorkflowRefusal,
    WorkflowRefusalReason,
)
from atelier2.contracts.workflows_v3 import WorkflowGraphV3
from atelier2.ports.durable_runs import (
    DurableAgentExecutorCapabilityUnavailable,
    DurableAnswerActorMismatch,
    DurableAnswerCreated,
    DurableAnswerExisting,
    DurableAnswerNodeMissing,
    DurableAnswerRevisionConflict,
    DurableAnswerRoundAnswered,
    DurableAnswerRunMissing,
    DurableAnswerStale,
    DurableAnswerStateConflict,
    DurablePublishedRunStarter,
    DurableRunCreated,
    DurableRunExisting,
    DurableRunFormatNotExecutable,
    DurableRunIdentityConflict,
    DurableRunRevisionMissing,
    DurableWriteUnavailable,
    StartPublishedRunRequest,
    TransactionalWaitAnswerer,
)
from atelier2.ports.durable_runs import (
    DurableStateCorrupt as PortDurableStateCorrupt,
)
from atelier2.ports.effects import (
    DurableReconciliationCommandConflict,
    DurableReconciliationCreated,
    DurableReconciliationDeterminationConflict,
    DurableReconciliationExisting,
    DurableReconciliationTargetMissing,
    TransactionalEffectReconcileCommander,
)
from atelier2.ports.published_revisions import (
    PublishedRevisionMissing,
    PublishedRevisionResolver,
    PublishedRevisionsUnavailable,
)
from atelier2.ports.workflow_revisions import (
    DurableRevisionCollision,
    DurableRevisionCreated,
    DurableRevisionExisting,
    WorkflowRevisionPublisher,
)
from tests.scenarios.api import permissive_projection_limit
from tests.scenarios.workflows import (
    V3_CONTROL_EDGE_LINE,
    V3_DOCUMENT,
    V3_WAIT_LINE_DOCUMENT,
    declared_output,
)


@dataclass
class FakePort:
    result: object

    def publish(self, _revision: WorkflowRevision) -> object:
        return self.result

    def resolve(self, _kind: object, _revision_hash: object) -> object:
        return self.result

    def start_published(self, _request: StartPublishedRunRequest) -> object:
        return self.result

    def submit_result(self, _request: object) -> object:
        return self.result


SMALL_DOCUMENT = V3_WAIT_LINE_DOCUMENT
REVISION = WorkflowRevision(b"format_version: nope")
HASH = WorkflowRevisionHash("0" * 64)
RUN = cast(Run, object())
ANSWER_VALUE = WaitAnswer(
    RunId("run"),
    HASH,
    "wait",
    NodeExecutionId.for_node(RunId("run"), HASH, "wait"),
    WaitAnswerActor.OPERATOR,
    b"3",
)
ANSWER = WaitAnswerSnapshot(ANSWER_VALUE, WaitAnswerState.PENDING, 0)
APPLIED_ANSWER = WaitAnswerSnapshot(ANSWER_VALUE, WaitAnswerState.APPLIED, 1)
COMMAND = cast(ReconcileCommand, object())


@pytest.mark.parametrize(
    ("port_result", "application_type"),
    [
        (DurableRevisionCreated(REVISION), PublicationCreated),
        (DurableRevisionExisting(REVISION), PublicationExisting),
        (DurableRevisionCollision(), PublicationCollision),
        (DurableWriteUnavailable(), WriteUnavailable),
        (PortDurableStateCorrupt(), DurableStateCorrupt),
    ],
)
def test_publication_maps_every_durable_result(
    port_result: object, application_type: type[object]
) -> None:
    result = publish_workflow_revision(
        SMALL_DOCUMENT,
        cast(WorkflowRevisionPublisher, FakePort(port_result)),
        parse_workflow_document,
        permissive_projection_limit(),
        cast(PublishedRevisionResolver, FakePort(PublishedRevisionMissing())),
    )

    assert isinstance(result, application_type)


def test_publication_rejects_invalid_yaml_before_the_write_port() -> None:
    result = publish_workflow_revision(
        b"!!python/object:unsafe {}",
        cast(WorkflowRevisionPublisher, FakePort(None)),
        parse_workflow_document,
        permissive_projection_limit(),
        cast(PublishedRevisionResolver, FakePort(PublishedRevisionMissing())),
    )

    assert isinstance(result, PublicationInvalid)


def test_publication_projects_a_valid_v3_document_it_reached_the_write_port_with() -> (
    None
):
    revision = WorkflowRevision(V3_DOCUMENT)

    result = publish_workflow_revision(
        V3_DOCUMENT,
        cast(WorkflowRevisionPublisher, FakePort(DurableRevisionCreated(revision))),
        parse_workflow_document,
        permissive_projection_limit(),
        cast(PublishedRevisionResolver, FakePort(PublishedRevisionMissing())),
    )

    assert isinstance(result, PublicationCreated)
    assert result.read.projection.revision == revision
    assert isinstance(result.read.projection.graph, WorkflowGraphV3)


def test_a_registry_that_cannot_answer_after_the_write_is_a_write_refusal() -> None:
    """The revision is stored; what could not be said about it is said as unavailable."""
    document = b"""format_version: 3
name: One agent
nodes:
  - id: implement
    type: agent
    role: builder
    mode: headless
    instruction: Do the one thing this chain is for.
""" + declared_output()
    revision = WorkflowRevision(document)

    result = publish_workflow_revision(
        document,
        cast(WorkflowRevisionPublisher, FakePort(DurableRevisionCreated(revision))),
        parse_workflow_document,
        permissive_projection_limit(),
        cast(
            PublishedRevisionResolver,
            FakePort(PublishedRevisionsUnavailable("registry asleep")),
        ),
    )

    assert result == WriteUnavailable("registry asleep")


def test_publication_refuses_an_invalid_v3_document_carrying_its_named_refusal() -> (
    None
):
    result = publish_workflow_revision(
        V3_DOCUMENT.replace(V3_CONTROL_EDGE_LINE, b""),
        cast(WorkflowRevisionPublisher, FakePort(None)),
        parse_workflow_document,
        permissive_projection_limit(),
        cast(PublishedRevisionResolver, FakePort(PublishedRevisionMissing())),
    )

    assert isinstance(result, PublicationInvalid)
    assert result.refusal == WorkflowRefusal(
        WorkflowRefusalReason.DATA_EDGE_OUTSIDE_CLOSURE,
        "inputs",
        "input 'candidate' reads node 'implement', which no depends_on edge "
        "orders before this node",
        "review",
    )
    assert result.detail == str(result.refusal)


def test_publication_returns_the_graph_validated_before_the_write() -> None:
    document = SMALL_DOCUMENT
    revision = WorkflowRevision(document)

    result = publish_workflow_revision(
        document,
        cast(
            WorkflowRevisionPublisher,
            FakePort(DurableRevisionCreated(revision)),
        ),
        parse_workflow_document,
        permissive_projection_limit(),
        cast(PublishedRevisionResolver, FakePort(PublishedRevisionMissing())),
    )

    assert isinstance(result, PublicationCreated)
    assert result.read.projection.revision == revision
    assert isinstance(result.read.projection.graph, WorkflowGraphV3)
    assert (
        result.read.projection.graph.name == "A person answers, then the line is done"
    )


@pytest.mark.proves("no-write-path-can-be-reached-with-a-dependency-left-out")
def test_a_publication_cannot_be_made_without_the_bound_it_applies() -> None:
    """There is no unbounded publication to reach any more.

    The bound used to be optional on the one call that writes a revision, so a
    caller could leave it out and the write would accept what a bound refuses.
    Leaving it out is not a permissive publication now — it is not a call.
    """
    with pytest.raises(TypeError):
        publish_workflow_revision(  # type: ignore[call-arg]
            SMALL_DOCUMENT,
            cast(WorkflowRevisionPublisher, FakePort(None)),
            parse_workflow_document,
        )


@pytest.mark.proves("no-write-path-can-be-reached-with-a-dependency-left-out")
def test_the_bound_a_publication_is_handed_is_the_bound_it_applies() -> None:
    """Making the bound required would be empty if it were not the one enforced."""
    revision = WorkflowRevision(SMALL_DOCUMENT)
    publisher = cast(
        WorkflowRevisionPublisher, FakePort(DurableRevisionCreated(revision))
    )
    tighter_than_the_document = WorkflowPublicationLimits(
        maximum_document_bytes=len(SMALL_DOCUMENT) - 1,
        maximum_nodes=100,
        maximum_string_characters=1_024,
        maximum_payload_bytes=49_152,
    )

    resolver = cast(PublishedRevisionResolver, FakePort(PublishedRevisionMissing()))

    published = publish_workflow_revision(
        SMALL_DOCUMENT,
        publisher,
        parse_workflow_document,
        permissive_projection_limit(),
        resolver,
    )
    refused = publish_workflow_revision(
        SMALL_DOCUMENT,
        publisher,
        parse_workflow_document,
        tighter_than_the_document,
        resolver,
    )

    assert isinstance(published, PublicationCreated)
    assert isinstance(refused, PublicationInvalid)


@pytest.mark.parametrize(
    ("port_result", "application_type"),
    [
        (DurableRunCreated(RUN), RunCreated),
        (DurableRunExisting(RUN), RunExisting),
        (DurableRunRevisionMissing(), RevisionMissing),
        (DurableRunIdentityConflict(), RunIdentityConflict),
        (
            DurableAgentExecutorCapabilityUnavailable(),
            AgentExecutorBindingUnavailable,
        ),
        (DurableRunFormatNotExecutable(), RunFormatNotExecutable),
        (DurableWriteUnavailable(), WriteUnavailable),
        (PortDurableStateCorrupt(), DurableStateCorrupt),
    ],
)
def test_start_maps_every_durable_result(
    port_result: object, application_type: type[object]
) -> None:
    result = start_published_run(
        RunId("run"),
        HASH,
        None,
        cast(DurablePublishedRunStarter, FakePort(port_result)),
    )

    assert isinstance(result, application_type)


@pytest.mark.parametrize(
    ("port_result", "application_type"),
    [
        (DurableAnswerCreated(ANSWER), AnswerAcceptedPending),
        (DurableAnswerExisting(ANSWER), AnswerExistingPending),
        (DurableAnswerExisting(APPLIED_ANSWER), AnswerExistingApplied),
        (
            DurableAnswerActorMismatch(WaitAnswerActor.OPERATOR),
            AnswerActorMismatch,
        ),
        (DurableAnswerRunMissing(), RunMissing),
        (DurableAnswerNodeMissing(), NodeMissing),
        (DurableAnswerRevisionConflict(), AnswerRevisionConflict),
        (DurableAnswerStateConflict(), AnswerStateConflict),
        (DurableAnswerRoundAnswered(), AnswerStateConflict),
        (DurableAnswerStale(), AnswerStale),
        (DurableWriteUnavailable(), WriteUnavailable),
        (PortDurableStateCorrupt(), DurableStateCorrupt),
    ],
)
def test_answer_maps_every_durable_result(
    port_result: object, application_type: type[object]
) -> None:
    result = answer_wait_result(
        RunId("run"),
        HASH,
        "waiting",
        NodeExecutionId.for_node(RunId("run"), HASH, "waiting"),
        WaitAnswerActor.OPERATOR,
        b"6",
        cast(TransactionalWaitAnswerer, FakePort(port_result)),
    )

    assert isinstance(result, application_type)


@pytest.mark.parametrize(
    ("port_result", "application_type"),
    [
        (
            DurableReconciliationCreated(
                ReconcileCommandSnapshot(COMMAND, ReconcileCommandState.PENDING)
            ),
            ReconciliationAcceptedPending,
        ),
        (
            DurableReconciliationCreated(
                ReconcileCommandSnapshot(
                    COMMAND, ReconcileCommandState.REJECTED_CONFLICT
                )
            ),
            ReconciliationStale,
        ),
        (
            DurableReconciliationExisting(
                ReconcileCommandSnapshot(COMMAND, ReconcileCommandState.PENDING)
            ),
            ReconciliationExistingPending,
        ),
        (
            DurableReconciliationExisting(
                ReconcileCommandSnapshot(COMMAND, ReconcileCommandState.APPLIED)
            ),
            ReconciliationExistingApplied,
        ),
        (
            DurableReconciliationExisting(
                ReconcileCommandSnapshot(
                    COMMAND, ReconcileCommandState.REJECTED_CONFLICT
                )
            ),
            ReconciliationExistingRejected,
        ),
        (DurableReconciliationTargetMissing(), ReconciliationTargetMissing),
        (DurableReconciliationCommandConflict(), ReconciliationCommandConflict),
        (
            DurableReconciliationDeterminationConflict(),
            ReconciliationDeterminationConflict,
        ),
        (DurableWriteUnavailable(), WriteUnavailable),
        (PortDurableStateCorrupt(), DurableStateCorrupt),
    ],
)
def test_reconciliation_maps_every_durable_result(
    port_result: object, application_type: type[object]
) -> None:
    result = reconcile_effect_result(
        COMMAND, cast(TransactionalEffectReconcileCommander, FakePort(port_result))
    )

    assert isinstance(result, application_type)
