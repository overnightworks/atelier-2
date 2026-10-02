"""What each write use-case decides, one case per outcome.

These carried no `proves` mark while the cancellation was still a pass-through:
the sentence they would have claimed -- every write decides through a use-case
that owns the store's answer -- was not true of this tree, and a mark would have
made the gate agree with a sentence the code did not keep. The cancellation is
translated now and its cases stand below with the rest, so the mark joins them.
"""

from __future__ import annotations

import ast
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from atelier2.adapters.markdown_agent_definitions import (
    parse_agent_definition,
    render_agent_definition,
)
from atelier2.application.answer_wait import UnanswerableWait, answer_wait_result
from atelier2.application.cancel_agent_attempt import (
    AttemptAlreadyTerminal,
    AttemptMissing,
    AttemptNotCurrent,
    CancellationAccepted,
    CancellationRunMissing,
    CancellationStale,
    CommandConflict,
    ReplacementNotAllowed,
    cancel_agent_attempt,
)
from atelier2.application.cancel_run import (
    CancelAccepted,
    CancelCommandConflict,
    CancelNotCancellable,
    CancelOvertakenBySuccess,
    CancelRunMissing,
    CancelTerminalRetry,
    MalformedIdempotencyKey,
    cancel_run_result,
)
from atelier2.application.publish_adapter_operation_revision import (
    AdapterOperationPublicationCollision,
    AdapterOperationPublicationCreated,
    AdapterOperationPublicationExisting,
    AdapterOperationPublicationInvalid,
    publish_adapter_operation_revision,
)
from atelier2.application.publish_agent_configurations import (
    PUBLISHED_CONFIGURATION_FORMAT,
    AgentConfigurationRevisionCollision,
    AgentConfigurationRevisionPublished,
    AgentConfigurationRevisionUnchanged,
    AgentExecutorBindingUnavailable,
    AuthProfileRevisionCollision,
    AuthProfileRevisionConflict,
    AuthProfileRevisionNotFound,
    AuthProfileRevisionPublished,
    AuthProfileRevisionUnchanged,
    UnpublishableAgentConfiguration,
    UnpublishableAuthProfile,
    publish_agent_configuration_revision,
    publish_auth_profile_revision,
)
from atelier2.application.publish_agent_definition_revision import (
    AgentDefinitionPublicationCollision,
    AgentDefinitionPublicationCreated,
    AgentDefinitionPublicationExisting,
    AgentDefinitionPublicationInvalid,
    publish_agent_definition_revision,
)
from atelier2.application.publish_budget_revision import (
    BudgetPublicationCollision,
    BudgetPublicationCreated,
    BudgetPublicationExisting,
    BudgetPublicationInvalid,
    publish_budget_revision,
)
from atelier2.application.publish_schema_revision import (
    SchemaPublicationCollision,
    SchemaPublicationCreated,
    SchemaPublicationExisting,
    SchemaPublicationInvalid,
    publish_schema_revision,
)
from atelier2.application.publish_tool_grant_revision import (
    ToolGrantPublicationCollision,
    ToolGrantPublicationCreated,
    ToolGrantPublicationExisting,
    ToolGrantPublicationInvalid,
    publish_tool_grant_revision,
)
from atelier2.application.refusals import DurableStateCorrupt, WriteUnavailable
from atelier2.application.start_published_run import (
    AuthoredAgentBinding,
    AuthoredOrder,
    InvalidAgentBindings,
    RunCreated,
    start_published_run,
)
from atelier2.contracts.adapter_operations_v3 import AdapterOperationRefusal
from atelier2.contracts.agent_attempts import (
    AgentAttempt,
    AgentAttemptCancellation,
    AgentAttemptId,
    AgentAttemptReplacement,
    AgentAttemptState,
    CancelAgentAttemptRequest,
)
from atelier2.contracts.agent_definitions import AgentDefinitionRefusal
from atelier2.contracts.agents import (
    AgentConfigurationRevisionFormatVersion,
    AgentExecutionRequestHash,
    AgentExecutorOperationalIdentity,
)
from atelier2.contracts.artifacts import ArtifactHash
from atelier2.contracts.budgets_v3 import BudgetRevisionRefusal
from atelier2.contracts.executions import NodeExecutionId, WaitAnswerActor
from atelier2.contracts.orders import ArtifactOrderValue
from atelier2.contracts.revisions_v3 import PublishedRevision, RevisionKind
from atelier2.contracts.run_cancellations import CancelRunRequest
from atelier2.contracts.run_projections import RunCancellationRefusal
from atelier2.contracts.runs import Run, RunId, RunState, WorkflowRevisionHash
from atelier2.contracts.schemas_v3 import SchemaDocumentRefusal
from atelier2.contracts.tool_grants_v3 import ToolGrantRefusal
from atelier2.ports.agent_attempts import (
    AgentAttemptCancellationAccepted as DurableCancellationAccepted,
)
from atelier2.ports.agent_attempts import (
    AgentAttemptCancellationCommandConflict as DurableCommandConflict,
)
from atelier2.ports.agent_attempts import (
    AgentAttemptCancellationNotCurrent as DurableNotCurrent,
)
from atelier2.ports.agent_attempts import (
    AgentAttemptCancellationRunMissing as DurableRunMissing,
)
from atelier2.ports.agent_attempts import (
    AgentAttemptCancellationStale as DurableStale,
)
from atelier2.ports.agent_attempts import (
    AgentAttemptCancellationTargetMissing as DurableTargetMissing,
)
from atelier2.ports.agent_attempts import (
    AgentAttemptCancellationTerminalConflict as DurableTerminalConflict,
)
from atelier2.ports.agent_attempts import (
    AgentAttemptReplacementNotAllowed as DurableReplacementNotAllowed,
)
from atelier2.ports.agent_attempts import (
    DurableWriteUnavailable as PortDurableWriteUnavailable,
)
from atelier2.ports.agent_attempts import (
    RunCancellationAccepted as DurableRunCancellationAccepted,
)
from atelier2.ports.agent_attempts import (
    RunCancellationCommandConflict as DurableRunCommandConflict,
)
from atelier2.ports.agent_attempts import (
    RunCancellationNotCancellable as DurableRunNotCancellable,
)
from atelier2.ports.agent_attempts import (
    RunCancellationOvertakenBySuccess as DurableRunOvertakenBySuccess,
)
from atelier2.ports.agent_attempts import (
    RunCancellationRunMissing as DurableRunCancellationRunMissing,
)
from atelier2.ports.agent_attempts import (
    RunCancellationTerminalRetry as DurableRunTerminalRetry,
)
from atelier2.ports.agent_configurations import (
    AgentConfigurationRevisionCollision as PortConfigurationCollision,
)
from atelier2.ports.agent_configurations import (
    AgentConfigurationRevisionCreated as PortConfigurationCreated,
)
from atelier2.ports.agent_configurations import (
    AgentConfigurationRevisionExisting as PortConfigurationExisting,
)
from atelier2.ports.agent_configurations import (
    AgentExecutorBindingUnavailable as PortExecutorBindingUnavailable,
)
from atelier2.ports.agent_configurations import (
    AuthProfileRevisionCollision as PortProfileCollision,
)
from atelier2.ports.agent_configurations import (
    AuthProfileRevisionConflict as PortProfileConflict,
)
from atelier2.ports.agent_configurations import (
    AuthProfileRevisionCreated as PortProfileCreated,
)
from atelier2.ports.agent_configurations import (
    AuthProfileRevisionExisting as PortProfileExisting,
)
from atelier2.ports.agent_configurations import (
    AuthProfileRevisionMissing as PortProfileMissing,
)
from atelier2.ports.durable_runs import (
    DurableAnswerCreated,
    DurableAnswerNotAdmitted,
    DurableRunCreated,
    DurableWriteUnavailable,
    StartPublishedRunRequestV3,
)
from atelier2.ports.durable_runs import (
    DurableStateCorrupt as PortDurableStateCorrupt,
)
from atelier2.ports.published_revisions import (
    PublishedRevisionCollision,
    PublishedRevisionCreated,
    PublishedRevisionExisting,
)

REVISION_HASH = WorkflowRevisionHash("a" * 64)
RUN_ID = RunId("run")
STORED: Any = object()
AUTH_PROFILE: Any = object()
RUN: Any = object()
SNAPSHOT: Any = object()


class ScriptedCatalog:
    """A catalog that answers each publication with the one answer a case scripts."""

    def __init__(self, answer: Any) -> None:
        self.answer = answer
        self.published: list[Any] = []

    def _record(self, revision: Any) -> Any:
        self.published.append(revision)
        return self.answer

    def publish_auth_profile_revision(self, revision: Any) -> Any:
        return self._record(revision)

    def publish_agent_configuration_revision(self, revision: Any) -> Any:
        return self._record(revision)

    def agent_configuration_revision(self, revision_hash: Any) -> Any:
        raise AssertionError("a publication under test read the catalog back")

    def list_agent_configuration_revisions(self, after: Any, limit: int) -> Any:
        raise AssertionError("a publication under test listed the catalog")

    def list_auth_profile_revisions(self, after: Any, limit: int) -> Any:
        raise AssertionError("a publication under test listed the catalog")


class ScriptedStarter:
    def __init__(self, answer: Any) -> None:
        self.answer = answer
        self.started: list[Any] = []

    def start_published(self, request: Any) -> Any:
        self.started.append(request)
        return self.answer


WRITE_REFUSALS = [
    (DurableWriteUnavailable(), WriteUnavailable()),
    (PortDurableStateCorrupt(), DurableStateCorrupt()),
]


def publish_profile(catalog: ScriptedCatalog) -> object:
    return publish_auth_profile_revision(
        "profile", 1, "anthropic", "subscription", catalog
    )


def publish_configuration(catalog: ScriptedCatalog) -> object:
    return publish_agent_configuration_revision(
        "claude", "b" * 64, "executor@1", "headless", catalog
    )


PUBLICATIONS: list[
    tuple[str, Callable[[ScriptedCatalog], object], list[tuple[Any, Any]]]
] = [
    (
        "auth-profile",
        publish_profile,
        [
            (PortProfileCreated(STORED), AuthProfileRevisionPublished(STORED)),
            (PortProfileExisting(STORED), AuthProfileRevisionUnchanged(STORED)),
            (PortProfileConflict(), AuthProfileRevisionConflict()),
            (PortProfileCollision(), AuthProfileRevisionCollision()),
            *WRITE_REFUSALS,
        ],
    ),
    (
        "agent-configuration",
        publish_configuration,
        [
            (
                PortConfigurationCreated(STORED, AUTH_PROFILE),
                AgentConfigurationRevisionPublished(STORED, AUTH_PROFILE),
            ),
            (
                PortConfigurationExisting(STORED, AUTH_PROFILE),
                AgentConfigurationRevisionUnchanged(STORED, AUTH_PROFILE),
            ),
            (PortProfileMissing(), AuthProfileRevisionNotFound()),
            (PortExecutorBindingUnavailable(), AgentExecutorBindingUnavailable()),
            (PortConfigurationCollision(), AgentConfigurationRevisionCollision()),
            *WRITE_REFUSALS,
        ],
    ),
]


@pytest.mark.proves("every-write-decision-belongs-to-a-use-case")
@pytest.mark.parametrize(
    ("publish", "port_answer", "expected"),
    [
        pytest.param(
            publish, port_answer, expected, id=f"{name}-{type(port_answer).__name__}"
        )
        for name, publish, outcomes in PUBLICATIONS
        for port_answer, expected in outcomes
    ],
)
def test_every_port_answer_of_a_publication_becomes_this_layers_own_outcome(
    publish: Callable[[ScriptedCatalog], object], port_answer: Any, expected: Any
) -> None:
    catalog = ScriptedCatalog(port_answer)

    assert publish(catalog) == expected
    assert len(catalog.published) == 1


@pytest.mark.parametrize(
    ("publish", "authored"),
    [
        (publish_auth_profile_revision, ("profile", 1, "anthropic", "not-a-mode")),
        (publish_agent_configuration_revision, ("claude", "short", "e@1", "headless")),
    ],
    ids=["unknown-auth-mode", "malformed-auth-profile-hash"],
)
def test_authored_values_that_make_no_revision_refuse_before_the_catalog_is_asked(
    publish: Callable[..., Any], authored: tuple[Any, ...]
) -> None:
    """The construction is the use-case's, so its failure is an outcome, not an
    exception a caller above has to know to catch — and nothing is published."""
    catalog = ScriptedCatalog(PortProfileCreated(STORED))

    result = publish(*authored, catalog)

    assert isinstance(
        result, (UnpublishableAuthProfile, UnpublishableAgentConfiguration)
    )
    assert catalog.published == []


SCHEMA_DOCUMENT = b'{"type": "object"}'
SCHEMA_REVISION = PublishedRevision(RevisionKind.SCHEMA, SCHEMA_DOCUMENT)
BUDGET_DOCUMENT = b'{"attempt_deadline_seconds": 900}'
BUDGET_REVISION = PublishedRevision(RevisionKind.BUDGET_POLICY, BUDGET_DOCUMENT)
TOOL_GRANT_DOCUMENT = b'{"capability": "run-project-verification"}'
TOOL_GRANT_REVISION = PublishedRevision(RevisionKind.TOOL, TOOL_GRANT_DOCUMENT)
ADAPTER_OPERATION_DOCUMENT = b'{"operation": "open-pr"}'
ADAPTER_OPERATION_REVISION = PublishedRevision(
    RevisionKind.ADAPTER_OPERATION, ADAPTER_OPERATION_DOCUMENT
)
AGENT_DEFINITION_DOCUMENT = (
    b"---\n"
    b"name: publication-witness\n"
    b"description: The definition this publication publishes.\n"
    b"---\n"
    b"\nWatch the publication.\n"
)
AGENT_DEFINITION_REVISION = PublishedRevision(
    RevisionKind.AGENT_DEFINITION, AGENT_DEFINITION_DOCUMENT
)


class ScriptedRegistry:
    """A published-revision registry that answers with the one scripted result."""

    def __init__(self, answer: Any) -> None:
        self.answer = answer
        self.published: list[PublishedRevision] = []

    def publish_revision(self, revision: PublishedRevision) -> Any:
        self.published.append(revision)
        return self.answer

    def resolve(self, kind: object, revision_hash: object) -> Any:
        del kind, revision_hash
        raise AssertionError("schema publication never resolves")


@dataclass(frozen=True)
class RevisionPublication:
    """One published kind's door, and the three words it answers the store in.

    Every door asks the same registry port and is answered from the same closed
    set. Only the vocabulary differs, so the vocabulary is the scenario and the
    port's answers are the table.
    """

    name: str
    publish: Callable[[ScriptedRegistry], object]
    revision: PublishedRevision
    created: Callable[[PublishedRevision], object]
    existing: Callable[[PublishedRevision], object]
    collision: Callable[[], object]


REVISION_PUBLICATIONS = (
    RevisionPublication(
        "schema",
        lambda registry: publish_schema_revision(SCHEMA_DOCUMENT, registry),
        SCHEMA_REVISION,
        SchemaPublicationCreated,
        SchemaPublicationExisting,
        SchemaPublicationCollision,
    ),
    RevisionPublication(
        "budget",
        lambda registry: publish_budget_revision(BUDGET_DOCUMENT, registry),
        BUDGET_REVISION,
        BudgetPublicationCreated,
        BudgetPublicationExisting,
        BudgetPublicationCollision,
    ),
    RevisionPublication(
        "tool-grant",
        lambda registry: publish_tool_grant_revision(TOOL_GRANT_DOCUMENT, registry),
        TOOL_GRANT_REVISION,
        ToolGrantPublicationCreated,
        ToolGrantPublicationExisting,
        ToolGrantPublicationCollision,
    ),
    RevisionPublication(
        "adapter-operation",
        lambda registry: publish_adapter_operation_revision(
            ADAPTER_OPERATION_DOCUMENT, registry
        ),
        ADAPTER_OPERATION_REVISION,
        AdapterOperationPublicationCreated,
        AdapterOperationPublicationExisting,
        AdapterOperationPublicationCollision,
    ),
    RevisionPublication(
        "agent-definition",
        lambda registry: publish_agent_definition_revision(
            AGENT_DEFINITION_DOCUMENT,
            parse_agent_definition,
            render_agent_definition,
            registry,
        ),
        AGENT_DEFINITION_REVISION,
        AgentDefinitionPublicationCreated,
        AgentDefinitionPublicationExisting,
        AgentDefinitionPublicationCollision,
    ),
)


def own_words(publication: RevisionPublication) -> list[tuple[Any, Any]]:
    """Every answer the registry port can give, and what this door calls it."""
    return [
        (
            PublishedRevisionCreated(publication.revision),
            publication.created(publication.revision),
        ),
        (
            PublishedRevisionExisting(publication.revision),
            publication.existing(publication.revision),
        ),
        (PublishedRevisionCollision(), publication.collision()),
        *WRITE_REFUSALS,
    ]


@pytest.mark.proves("every-write-decision-belongs-to-a-use-case")
@pytest.mark.parametrize(
    ("publication", "port_answer", "expected"),
    [
        pytest.param(
            publication,
            port_answer,
            expected,
            id=f"{publication.name}-{type(port_answer).__name__}",
        )
        for publication in REVISION_PUBLICATIONS
        for port_answer, expected in own_words(publication)
    ],
)
def test_every_port_answer_of_a_revision_publication_becomes_this_doors_own_word(
    publication: RevisionPublication, port_answer: Any, expected: Any
) -> None:
    registry = ScriptedRegistry(port_answer)

    assert publication.publish(registry) == expected
    assert registry.published == [publication.revision]


def test_a_schema_outside_the_profile_is_refused_before_the_store_is_asked() -> None:
    registry = ScriptedRegistry(PublishedRevisionCreated(SCHEMA_REVISION))

    result = publish_schema_revision(b"Guten Morgen", registry)

    assert isinstance(result, SchemaPublicationInvalid)
    assert result.verdict.refusal is SchemaDocumentRefusal.DOCUMENT_NOT_JSON
    assert registry.published == []


@pytest.mark.proves("a-published-budget-bounds-an-attempt-or-is-refused-by-name")
def test_a_budget_bounding_nothing_is_refused_before_the_store_is_asked() -> None:
    registry = ScriptedRegistry(PublishedRevisionCreated(BUDGET_REVISION))

    result = publish_budget_revision(b'{"maximum_assistant_turns": 8}', registry)

    assert isinstance(result, BudgetPublicationInvalid)
    assert result.verdict.reason is BudgetRevisionRefusal.MISSING_ATTEMPT_DEADLINE
    assert registry.published == []


def test_a_grant_this_runtime_cannot_redeem_is_refused_before_the_store_is_asked() -> (
    None
):
    registry = ScriptedRegistry(PublishedRevisionCreated(TOOL_GRANT_REVISION))

    result = publish_tool_grant_revision(b"{}", registry)

    assert isinstance(result, ToolGrantPublicationInvalid)
    assert result.verdict.reason is ToolGrantRefusal.MISSING_CAPABILITY
    assert registry.published == []


def test_an_operation_this_runtime_cannot_perform_is_refused_before_the_store_is_asked() -> (
    None
):
    registry = ScriptedRegistry(PublishedRevisionCreated(ADAPTER_OPERATION_REVISION))

    result = publish_adapter_operation_revision(b"{}", registry)

    assert isinstance(result, AdapterOperationPublicationInvalid)
    assert result.verdict.reason is AdapterOperationRefusal.MISSING_OPERATION
    assert registry.published == []


def test_a_definition_no_author_could_have_written_is_refused_before_the_store() -> (
    None
):
    registry = ScriptedRegistry(PublishedRevisionCreated(AGENT_DEFINITION_REVISION))

    result = publish_agent_definition_revision(
        b"Guten Morgen", parse_agent_definition, render_agent_definition, registry
    )

    assert isinstance(result, AgentDefinitionPublicationInvalid)
    assert result.verdict.refusal is AgentDefinitionRefusal.FRONTMATTER_MISSING
    assert registry.published == []


PUBLICATION_ANSWERS_OF_THE_REGISTRY_PORT = frozenset(
    {
        "PublishedRevisionCreated",
        "PublishedRevisionExisting",
        "PublishedRevisionCollision",
    }
)
PUBLISHED_REVISIONS_PORT = "atelier2.ports.published_revisions"
APPLICATION_SOURCE = Path(__file__).parents[2] / "src" / "atelier2" / "application"


def publication_answers_named_by(module: Path) -> set[str]:
    """Which of the port's publication answers this module can name at all.

    Imports rather than usages, so an alias cannot hide one: a module that never
    imports the answer has no way to match on it.
    """
    tree = ast.parse(module.read_text(encoding="utf-8"), filename=str(module))
    return {
        imported.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == PUBLISHED_REVISIONS_PORT
        for imported in node.names
    } & PUBLICATION_ANSWERS_OF_THE_REGISTRY_PORT


def test_the_registrys_publication_answers_are_read_by_exactly_one_use_case() -> None:
    """A sixth answer from the port must break one module, not five.

    An exhaustive match is a promise that every answer the store can give was
    considered. Five copies of it are five places to keep that promise, and the
    forgotten one is the one that quietly answers something else.
    """
    readers = {
        module.name
        for module in APPLICATION_SOURCE.glob("*.py")
        if publication_answers_named_by(module)
    }

    assert readers == {"publish_document_revision.py"}


def test_a_configuration_is_recorded_under_the_format_this_publication_decides() -> (
    None
):
    """No caller chooses the format, so the use-case owns it rather than a route."""
    catalog = ScriptedCatalog(PortConfigurationCreated(STORED, AUTH_PROFILE))

    publish_configuration(catalog)

    assert (
        catalog.published[0].revision_format_version is PUBLISHED_CONFIGURATION_FORMAT
    )
    assert PUBLISHED_CONFIGURATION_FORMAT is AgentConfigurationRevisionFormatVersion.V2


def test_a_start_that_binds_no_agent_asks_for_the_run_without_a_binding_set() -> None:
    starter = ScriptedStarter(DurableRunCreated(RUN))

    assert start_published_run(RUN_ID, REVISION_HASH, None, starter) == RunCreated(RUN)
    assert not hasattr(starter.started[0], "agent_bindings")


def test_a_start_whose_authored_binding_is_no_binding_refuses_before_the_store() -> (
    None
):
    """Building the durable request is part of the decision, so a role that is not
    one refuses the start in the same vocabulary as everything else — and the store
    is never asked."""
    starter = ScriptedStarter(DurableRunCreated(RUN))

    result = start_published_run(
        RUN_ID, REVISION_HASH, (AuthoredAgentBinding("", "c" * 64),), starter
    )

    assert result == InvalidAgentBindings()
    assert starter.started == []


def test_a_start_that_binds_an_agent_carries_the_authored_roles_to_the_store() -> None:
    starter = ScriptedStarter(DurableRunCreated(RUN))

    start_published_run(
        RUN_ID,
        REVISION_HASH,
        (AuthoredAgentBinding("builder", "c" * 64),),
        starter,
    )

    bound = starter.started[0].agent_bindings
    assert [binding.role.value for binding in bound.bindings] == ["builder"]


def test_a_start_that_carries_orders_asks_for_the_v3_shape_without_a_schema_hash() -> (
    None
):
    """The caller names the material. The start pins the schema the document named."""
    starter = ScriptedStarter(DurableRunCreated(RUN))

    start_published_run(
        RUN_ID,
        REVISION_HASH,
        (AuthoredAgentBinding("builder", "c" * 64),),
        starter,
        (AuthoredOrder("order", ArtifactOrderValue(ArtifactHash("d" * 64))),),
    )

    requested = starter.started[0]
    assert isinstance(requested, StartPublishedRunRequestV3)
    assert requested.run_inputs == ()
    assert [(order.name, order.value) for order in requested.orders] == [
        ("order", ArtifactOrderValue(ArtifactHash("d" * 64)))
    ]


class ScriptedAnswerer:
    def __init__(self, answer: Any) -> None:
        self.answer = answer
        self.submitted: list[Any] = []

    def submit_result(self, request: Any) -> Any:
        self.submitted.append(request)
        return self.answer


def test_an_answer_that_makes_no_submission_refuses_before_the_store_is_asked() -> None:
    """Building the submission belongs to the decision, so a value that makes none
    is an outcome of it — and the store is never asked."""
    answerer = ScriptedAnswerer(DurableAnswerCreated(SNAPSHOT))

    result = answer_wait_result(
        RUN_ID,
        REVISION_HASH,
        "",
        NodeExecutionId.for_node(RUN_ID, REVISION_HASH, "waiting"),
        WaitAnswerActor.OPERATOR,
        b"6",
        answerer,
    )

    assert isinstance(result, UnanswerableWait)
    assert answerer.submitted == []


def test_bytes_the_waiting_node_does_not_admit_read_back_as_an_unanswerable_wait() -> (
    None
):
    """Which vocabulary the node declares is the store's to know, not this layer's.

    A V1 wait admits canonical integer text and a V3 wait admits what its own
    schema admits, so the bytes are carried to the store and its refusal is what
    decides. The caller is told the same thing either way: this was no answer.
    """
    answerer = ScriptedAnswerer(DurableAnswerNotAdmitted("not what this node admits"))

    result = answer_wait_result(
        RUN_ID,
        REVISION_HASH,
        "waiting",
        NodeExecutionId.for_node(RUN_ID, REVISION_HASH, "waiting"),
        WaitAnswerActor.OPERATOR,
        b"06",
        answerer,
    )

    assert isinstance(result, UnanswerableWait)
    assert [submitted.answer_bytes for submitted in answerer.submitted] == [b"06"]


def test_an_answer_carries_the_authored_values_into_the_submission() -> None:
    answerer = ScriptedAnswerer(DurableAnswerCreated(SNAPSHOT))

    answer_wait_result(
        RUN_ID,
        REVISION_HASH,
        "waiting",
        NodeExecutionId.for_node(RUN_ID, REVISION_HASH, "waiting"),
        WaitAnswerActor.OPERATOR,
        b"6",
        answerer,
    )

    submitted = answerer.submitted[0]
    assert (submitted.run_id, submitted.node_id, submitted.answer_bytes) == (
        RUN_ID,
        "waiting",
        b"6",
    )


class ScriptedCanceller:
    """One canceller that answers with exactly what it was scripted to say."""

    def __init__(self, answer: Any) -> None:
        self._answer = answer
        self.asked: list[CancelAgentAttemptRequest] = []

    def request_cancellation(self, request: CancelAgentAttemptRequest) -> Any:
        self.asked.append(request)
        return self._answer

    def request_run_cancellation(self, request: CancelRunRequest) -> Any:
        raise AssertionError("the attempt use-case under test asked the run command")


# Derived rather than invented: the attempt id is bound to its execution and
# request, so a made-up one is refused before any use-case is reached.
CANCELLED_EXECUTION = NodeExecutionId("b" * 64)
CANCELLED_REQUEST_HASH = AgentExecutionRequestHash("c" * 64)
CANCELLED_ATTEMPT_ID = AgentAttemptId.for_execution(
    CANCELLED_EXECUTION, CANCELLED_REQUEST_HASH
)
CANCELLATION_REQUEST = CancelAgentAttemptRequest(
    RunId("run/cancel"),
    CANCELLED_ATTEMPT_ID,
    "command-1",
    1,
    AgentAttemptReplacement.NONE,
)
CANCELLED_ATTEMPT = AgentAttempt(
    CANCELLED_ATTEMPT_ID,
    CANCELLED_EXECUTION,
    CANCELLED_REQUEST_HASH,
    AgentExecutorOperationalIdentity("exact-operation"),
    RunId("run/cancel"),
    WorkflowRevisionHash("d" * 64),
    "implement",
    1,
    AgentAttemptState.CANCEL_REQUESTED,
    1,
    cancellation=AgentAttemptCancellation("command-1", 1, AgentAttemptReplacement.NONE),
)


@pytest.mark.proves("every-write-decision-belongs-to-a-use-case")
@pytest.mark.parametrize(
    ("port_answer", "expected"),
    [
        pytest.param(
            DurableCancellationAccepted(CANCELLED_ATTEMPT, False),
            CancellationAccepted(CANCELLED_ATTEMPT, False),
            id="accepted",
        ),
        pytest.param(DurableRunMissing(), CancellationRunMissing(), id="run-missing"),
        pytest.param(DurableTargetMissing(), AttemptMissing(), id="attempt-missing"),
        pytest.param(DurableNotCurrent(), AttemptNotCurrent(), id="not-current"),
        pytest.param(DurableStale(), CancellationStale(), id="stale"),
        pytest.param(
            DurableTerminalConflict(), AttemptAlreadyTerminal(), id="already-terminal"
        ),
        pytest.param(
            DurableCommandConflict(), CommandConflict(), id="command-conflict"
        ),
        pytest.param(
            DurableReplacementNotAllowed(),
            ReplacementNotAllowed(),
            id="replacement-not-allowed",
        ),
        pytest.param(
            PortDurableWriteUnavailable(), WriteUnavailable(), id="write-unavailable"
        ),
        pytest.param(
            PortDurableStateCorrupt(), DurableStateCorrupt(), id="state-corrupt"
        ),
    ],
)
def test_every_port_answer_of_a_cancellation_becomes_this_layers_own_outcome(
    port_answer: Any, expected: Any
) -> None:
    """The last write to be translated, answered in this layer's words.

    It handed the store's union straight to the route until now, which is what
    made the application layer a corridor for this one call: the route read the
    store's vocabulary and the record that binds use cases named a port type.
    """
    canceller = ScriptedCanceller(port_answer)

    assert cancel_agent_attempt(CANCELLATION_REQUEST, canceller) == expected
    assert canceller.asked == [CANCELLATION_REQUEST]


class ScriptedRunCanceller:
    """One run canceller that answers with exactly what it was scripted to say."""

    def __init__(self, answer: Any) -> None:
        self._answer = answer
        self.asked: list[CancelRunRequest] = []

    def request_run_cancellation(self, request: CancelRunRequest) -> Any:
        self.asked.append(request)
        return self._answer

    def request_cancellation(self, request: CancelAgentAttemptRequest) -> Any:
        raise AssertionError("the run use-case under test asked the attempt command")


RUN_CANCEL_RUN_ID = RunId("run/cancel")
RUN_CANCEL_NODE_EXECUTION = NodeExecutionId.for_node(
    RUN_CANCEL_RUN_ID, WorkflowRevisionHash("d" * 64), "implement"
)
RUN_CANCEL_REQUEST = CancelRunRequest(
    RUN_CANCEL_RUN_ID, "operator-cancel-1", RUN_CANCEL_NODE_EXECUTION
)
RUN_CANCEL_CANONICAL_RUN = Run(
    RUN_CANCEL_RUN_ID,
    WorkflowRevisionHash("d" * 64),
    RunState.STARTED,
    "implement",
    3,
    5,
)


@pytest.mark.proves("every-write-decision-belongs-to-a-use-case")
@pytest.mark.parametrize(
    ("port_answer", "expected"),
    [
        pytest.param(
            DurableRunCancellationAccepted(CANCELLED_ATTEMPT),
            CancelAccepted(CANCELLED_ATTEMPT),
            id="accepted",
        ),
        pytest.param(
            DurableRunTerminalRetry(RUN_CANCEL_CANONICAL_RUN),
            CancelTerminalRetry(RUN_CANCEL_CANONICAL_RUN),
            id="terminal-retry",
        ),
        pytest.param(
            DurableRunOvertakenBySuccess(RUN_CANCEL_CANONICAL_RUN),
            CancelOvertakenBySuccess(RUN_CANCEL_CANONICAL_RUN),
            id="overtaken-by-success",
        ),
        pytest.param(
            DurableRunNotCancellable(RunCancellationRefusal.BETWEEN_NODES),
            CancelNotCancellable(RunCancellationRefusal.BETWEEN_NODES),
            id="not-cancellable",
        ),
        pytest.param(
            DurableRunCommandConflict(),
            CancelCommandConflict(),
            id="command-conflict",
        ),
        pytest.param(
            DurableRunCancellationRunMissing(),
            CancelRunMissing(),
            id="run-missing",
        ),
        pytest.param(
            PortDurableWriteUnavailable(), WriteUnavailable(), id="write-unavailable"
        ),
        pytest.param(
            PortDurableStateCorrupt(), DurableStateCorrupt(), id="state-corrupt"
        ),
    ],
)
def test_every_port_answer_of_a_run_cancellation_becomes_this_layers_own_outcome(
    port_answer: Any, expected: Any
) -> None:
    """#439 P2's use-case, held to the same discipline as its attempt sibling.

    A route above must never read a port word directly, so every answer the
    store can give this new command is proven to reach exactly one outcome in
    this layer's own vocabulary.
    """
    canceller = ScriptedRunCanceller(port_answer)

    result = cancel_run_result(
        RUN_CANCEL_REQUEST.run_id,
        RUN_CANCEL_REQUEST.idempotency_key,
        RUN_CANCEL_REQUEST.expected_node_execution_id,
        canceller,
    )

    assert result == expected
    assert canceller.asked == [RUN_CANCEL_REQUEST]


def test_a_malformed_idempotency_key_is_refused_before_the_store_is_asked() -> None:
    canceller = ScriptedRunCanceller(object())

    result = cancel_run_result(
        RUN_CANCEL_RUN_ID, "", RUN_CANCEL_NODE_EXECUTION, canceller
    )

    assert isinstance(result, MalformedIdempotencyKey)
    assert canceller.asked == []
