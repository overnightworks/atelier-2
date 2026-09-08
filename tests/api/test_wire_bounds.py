"""Every length bound the wire enforces belongs to the contract that owns it.

The wire is the outermost edge of the same fields the store already bounds. When
it types its own number, the two agree only until one of them moves: the contract
could widen `role` and the API would still refuse at the old width, refusing
input the durable side would have accepted, and no test would notice. That is not
hypothetical shape -- the same drift is what the schema-side bound-ownership
tests exist to catch, and this is the wire's half of it.

Two tests, because there are two ways to drift:

  - a bound whose value stopped matching its owner, or a newly bounded field
    nobody declared an owner for, and
  - a bound typed as a literal, which cannot follow its owner at all.
"""

from __future__ import annotations

import ast
import inspect
from collections.abc import Iterator, Mapping
from pathlib import Path
from types import ModuleType

import pytest
from annotated_types import MaxLen
from pydantic import BaseModel, ValidationError

from atelier2.api.references import (
    MAXIMUM_INVALID_FIELD_PATH_CHARACTERS,
    MAXIMUM_INVALID_FIELD_REASON_CHARACTERS,
    MAXIMUM_NODE_INSTRUCTION_PREVIEW_CHARACTERS,
    MAXIMUM_PUBLIC_DEFINITION_SOURCE_REFERENCE_CHARACTERS,
    MAXIMUM_PUBLIC_PROJECT_REFERENCE_CHARACTERS,
    MAXIMUM_PUBLIC_SOURCE_REFERENCE_CHARACTERS,
    MAXIMUM_REFUSED_OUTPUT_BASE64_CHARACTERS,
    MAXIMUM_RUN_AGENT_BINDINGS,
    MAXIMUM_RUN_ORDERS,
    MAXIMUM_RUN_TERMINAL_ANSWER_BASE64_CHARACTERS,
)
from atelier2.api.wire import events, library, queue, requests, resources
from atelier2.contracts.agent_definitions import (
    MAXIMUM_AGENT_DEFINITION_DOCUMENT_CHARACTERS,
    MAXIMUM_AGENT_DEFINITION_TOOL_COUNT,
)
from atelier2.contracts.agent_transcripts import MAXIMUM_TRANSCRIPT_STEP_CHARACTERS
from atelier2.contracts.agents import (
    MAXIMUM_AGENT_FIELD_CHARACTERS,
    MAXIMUM_PROVIDER_ID_CHARACTERS,
)
from atelier2.contracts.catalog_v3 import (
    MAXIMUM_CATALOG_ACTOR_CHARACTERS,
    MAXIMUM_LINEAGE_DISPLAY_NAME_CHARACTERS,
)
from atelier2.contracts.definition_sources import MAXIMUM_REPOSITORY_PATH_CHARACTERS
from atelier2.contracts.host_configuration import (
    MAXIMUM_ACTIVE_PROJECT_SOURCES,
    MAXIMUM_EXACT_MODEL_ID_CHARACTERS,
    MAXIMUM_MODEL_REGISTRY_ENTRIES,
    MAXIMUM_PROJECT_ID_CHARACTERS,
    MAXIMUM_PROJECT_MODEL_DEFAULTS,
    MAXIMUM_SERVED_PROJECTS,
    MAXIMUM_SOURCE_ADDRESS_CHARACTERS,
    MAXIMUM_SOURCE_KIND_CHARACTERS,
    MAXIMUM_SOURCE_TOKEN_CHARACTERS,
)
from atelier2.contracts.queue_projection import (
    MAXIMUM_QUEUE_ADMISSION_RATIONALE_CHARACTERS,
    MAXIMUM_QUEUE_AUTOMATION_LABEL_CHARACTERS,
    MAXIMUM_QUEUE_ITEM_TITLE_CHARACTERS,
    MAXIMUM_TRACKER_ITEM_REFERENCE_CHARACTERS,
)
from atelier2.contracts.run_forks import MAXIMUM_RUN_FORK_SUCCESSORS
from atelier2.contracts.run_projections import MAXIMUM_RUN_ROW_DEFECT_DETAIL_CHARACTERS
from atelier2.contracts.schemas_v3 import MAXIMUM_INSTANCE_DOCUMENT_BYTES

WIRE_MODULES: tuple[ModuleType, ...] = (requests, resources, events, library, queue)

# Which owner each bounded wire field answers to. Three of them are contracts the
# durable side already obeys; the fourth is the wire's own, because no durable
# owner caps how many roles one run binds.
OWNED_WIRE_BOUNDS: Mapping[str, int] = {
    "AgentDefinitionRevisionDetailResource.system_prompt": (
        MAXIMUM_AGENT_DEFINITION_DOCUMENT_CHARACTERS
    ),
    "AgentDefinitionRevisionDetailResource.tools": MAXIMUM_AGENT_DEFINITION_TOOL_COUNT,
    "AgentBindingResourceV2.executor_revision": MAXIMUM_AGENT_FIELD_CHARACTERS,
    "AgentBindingResourceV2.model": MAXIMUM_AGENT_FIELD_CHARACTERS,
    "AgentBindingResourceV2.profile_id": MAXIMUM_AGENT_FIELD_CHARACTERS,
    "AgentBindingResourceV2.provider_id": MAXIMUM_PROVIDER_ID_CHARACTERS,
    "NodeProvenanceResource.auth_mode": MAXIMUM_AGENT_FIELD_CHARACTERS,
    "NodeProvenanceResource.executor_operational_identity": (
        MAXIMUM_AGENT_FIELD_CHARACTERS
    ),
    "NodeProvenanceResource.executor_revision": MAXIMUM_AGENT_FIELD_CHARACTERS,
    "NodeProvenanceResource.model": MAXIMUM_AGENT_FIELD_CHARACTERS,
    "NodeProvenanceResource.profile_id": MAXIMUM_AGENT_FIELD_CHARACTERS,
    "NodeProvenanceResource.provider_id": MAXIMUM_PROVIDER_ID_CHARACTERS,
    "NodeProvenanceResource.role": MAXIMUM_AGENT_FIELD_CHARACTERS,
    "AgentBindingResourceV2.role": MAXIMUM_AGENT_FIELD_CHARACTERS,
    "AdmitCatalogMemberRequestResource.actor": MAXIMUM_CATALOG_ACTOR_CHARACTERS,
    "AgentCancelRequestedEventResourceV3.command_id": MAXIMUM_AGENT_FIELD_CHARACTERS,
    "AgentCancelledEventResourceV3.command_id": MAXIMUM_AGENT_FIELD_CHARACTERS,
    "WaitCancelledEventResourceV3.command_id": MAXIMUM_AGENT_FIELD_CHARACTERS,
    "CatalogAdmissionResource.display_name": (MAXIMUM_LINEAGE_DISPLAY_NAME_CHARACTERS),
    "FoundCatalogLineageRequestResource.actor": MAXIMUM_CATALOG_ACTOR_CHARACTERS,
    "FoundCatalogLineageRequestResource.display_name": (
        MAXIMUM_LINEAGE_DISPLAY_NAME_CHARACTERS
    ),
    "RetireCatalogLineageRequestResource.actor": MAXIMUM_CATALOG_ACTOR_CHARACTERS,
    "AgentConfigurationRevisionResource.executor_revision": (
        MAXIMUM_AGENT_FIELD_CHARACTERS
    ),
    "AgentConfigurationRevisionResource.model": MAXIMUM_AGENT_FIELD_CHARACTERS,
    "AgentConfigurationRevisionResource.provider_id": MAXIMUM_PROVIDER_ID_CHARACTERS,
    "AgentConfigurationRevisionListItemResource.executor_revision": (
        MAXIMUM_AGENT_FIELD_CHARACTERS
    ),
    "AgentConfigurationRevisionListItemResource.model": MAXIMUM_AGENT_FIELD_CHARACTERS,
    "AgentConfigurationRevisionListItemResource.provider_id": (
        MAXIMUM_PROVIDER_ID_CHARACTERS
    ),
    "AgentInterruptedEventResourceV3.command_id": MAXIMUM_AGENT_FIELD_CHARACTERS,
    "AuthProfileRevisionResource.profile_id": MAXIMUM_AGENT_FIELD_CHARACTERS,
    "RecognizedAgentDefinitionResource.provider_id": MAXIMUM_PROVIDER_ID_CHARACTERS,
    "AuthProfileRevisionResource.provider_id": MAXIMUM_PROVIDER_ID_CHARACTERS,
    "CancelAgentAttemptRequestResource.command_id": MAXIMUM_AGENT_FIELD_CHARACTERS,
    "CancelRunRequestResource.idempotency_key": MAXIMUM_AGENT_FIELD_CHARACTERS,
    "ForkRunRequestResource.idempotency_key": MAXIMUM_AGENT_FIELD_CHARACTERS,
    "ForkRunRequestResource.restart_from_node_id": MAXIMUM_AGENT_FIELD_CHARACTERS,
    "CatalogNameResolutionResource.display_name": (
        MAXIMUM_LINEAGE_DISPLAY_NAME_CHARACTERS
    ),
    "PublishAgentConfigurationRevisionRequestResource.executor_revision": (
        MAXIMUM_AGENT_FIELD_CHARACTERS
    ),
    "PublishAgentConfigurationRevisionRequestResource.model": (
        MAXIMUM_AGENT_FIELD_CHARACTERS
    ),
    "PublishAuthProfileRevisionRequestResource.profile_id": (
        MAXIMUM_AGENT_FIELD_CHARACTERS
    ),
    "PublishAuthProfileRevisionRequestResource.provider_id": (
        MAXIMUM_PROVIDER_ID_CHARACTERS
    ),
    "RunResourceV3.agent_bindings": MAXIMUM_RUN_AGENT_BINDINGS,
    "RunResourceV3.fork_successors": MAXIMUM_RUN_FORK_SUCCESSORS,
    "RunResourceV3.orders": MAXIMUM_RUN_ORDERS,
    "RunResourceV3.work_item_reference": MAXIMUM_TRACKER_ITEM_REFERENCE_CHARACTERS,
    "RunTerminalAnswerValueResource.value_base64": (
        MAXIMUM_RUN_TERMINAL_ANSWER_BASE64_CHARACTERS
    ),
    # A document declares no more roles than a run can bind: one role is one
    # binding, so the two carry the same limit for the same reason.
    "WorkflowGraphResourceV3.agent_roles": MAXIMUM_RUN_AGENT_BINDINGS,
    "WorkflowNodePreviewResourceV3.instruction_start": (
        MAXIMUM_NODE_INSTRUCTION_PREVIEW_CHARACTERS
    ),
    "WorkflowNodePreviewResourceV3.role": MAXIMUM_AGENT_FIELD_CHARACTERS,
    "StartRunRequestResourceV2.agent_bindings": MAXIMUM_RUN_AGENT_BINDINGS,
    "StartRunRequestResourceV3.agent_bindings": MAXIMUM_RUN_AGENT_BINDINGS,
    "InlineOrderResource.value": MAXIMUM_INSTANCE_DOCUMENT_BYTES,
    "WorkItemOrderResource.work_item": MAXIMUM_TRACKER_ITEM_REFERENCE_CHARACTERS,
    "StartRunAgentBindingResourceV2.role": MAXIMUM_AGENT_FIELD_CHARACTERS,
    "ModelRegistryEntryResource.model_id": MAXIMUM_EXACT_MODEL_ID_CHARACTERS,
    "ModelRegistryRevisionResource.provider_id": MAXIMUM_PROVIDER_ID_CHARACTERS,
    "ModelRegistryRevisionResource.entries": MAXIMUM_MODEL_REGISTRY_ENTRIES,
    "ModelRegistryEntryInputResource.model_id": MAXIMUM_EXACT_MODEL_ID_CHARACTERS,
    "PutModelRegistryRevisionRequestResource.entries": MAXIMUM_MODEL_REGISTRY_ENTRIES,
    "ProjectModelDefaultResource.provider_id": MAXIMUM_PROVIDER_ID_CHARACTERS,
    "ProjectModelDefaultResource.model_id": MAXIMUM_EXACT_MODEL_ID_CHARACTERS,
    "ProjectModelDefaultInputResource.provider_id": MAXIMUM_PROVIDER_ID_CHARACTERS,
    "ProjectModelDefaultInputResource.model_id": MAXIMUM_EXACT_MODEL_ID_CHARACTERS,
    "ProjectModelDefaultsRevisionResource.project_id": MAXIMUM_PROJECT_ID_CHARACTERS,
    "ProjectModelDefaultsRevisionResource.public_project_reference": (
        MAXIMUM_PUBLIC_PROJECT_REFERENCE_CHARACTERS
    ),
    "ProjectModelDefaultsRevisionResource.defaults": MAXIMUM_PROJECT_MODEL_DEFAULTS,
    "PutProjectModelDefaultsRevisionRequestResource.defaults": (
        MAXIMUM_PROJECT_MODEL_DEFAULTS
    ),
    "RoleModelResolutionResource.role": MAXIMUM_AGENT_FIELD_CHARACTERS,
    "RoleModelResolutionResource.model_id": MAXIMUM_EXACT_MODEL_ID_CHARACTERS,
    "RoleModelResolutionResource.family_differs_from": (MAXIMUM_AGENT_FIELD_CHARACTERS),
    "ProjectModelResolutionResource.project_id": MAXIMUM_PROJECT_ID_CHARACTERS,
    "ProjectModelResolutionResource.public_project_reference": (
        MAXIMUM_PUBLIC_PROJECT_REFERENCE_CHARACTERS
    ),
    "ProjectModelResolutionResource.resolutions": MAXIMUM_RUN_AGENT_BINDINGS,
    "ModelResolutionOverrideResource.role": MAXIMUM_AGENT_FIELD_CHARACTERS,
    "ResolveProjectModelsRequestResource.overrides": MAXIMUM_RUN_AGENT_BINDINGS,
    "ProjectResource.public_project_reference": (
        MAXIMUM_PUBLIC_PROJECT_REFERENCE_CHARACTERS
    ),
    "ProjectListResource.items": MAXIMUM_SERVED_PROJECTS,
    "ProjectSourceConnectionRevisionResource.public_project_reference": (
        MAXIMUM_PUBLIC_PROJECT_REFERENCE_CHARACTERS
    ),
    "ProjectSourceConnectionRevisionResource.source_kind": (
        MAXIMUM_SOURCE_KIND_CHARACTERS
    ),
    "ProjectSourceConnectionRevisionResource.source_address": (
        MAXIMUM_SOURCE_ADDRESS_CHARACTERS
    ),
    "ConnectProjectSourceRequestResource.address": MAXIMUM_SOURCE_ADDRESS_CHARACTERS,
    "ConnectProjectSourceRequestResource.token": MAXIMUM_SOURCE_TOKEN_CHARACTERS,
    "RotateProjectSourceTokenRequestResource.token": MAXIMUM_SOURCE_TOKEN_CHARACTERS,
    "ProjectSourceResource.public_source_reference": (
        MAXIMUM_PUBLIC_SOURCE_REFERENCE_CHARACTERS
    ),
    "ProjectSourceResource.kind": MAXIMUM_SOURCE_KIND_CHARACTERS,
    "ProjectSourceResource.address": MAXIMUM_SOURCE_ADDRESS_CHARACTERS,
    "ProjectSourceListResource.items": MAXIMUM_ACTIVE_PROJECT_SOURCES,
    "SkippedProjectSourceItemResource.tracker_item_reference": (
        MAXIMUM_TRACKER_ITEM_REFERENCE_CHARACTERS
    ),
    "InvalidFieldResource.path": MAXIMUM_INVALID_FIELD_PATH_CHARACTERS,
    "InvalidFieldResource.reason": MAXIMUM_INVALID_FIELD_REASON_CHARACTERS,
    "UncastRoleResource.role": MAXIMUM_AGENT_FIELD_CHARACTERS,
    "UncastRoleResource.family_differs_from": MAXIMUM_AGENT_FIELD_CHARACTERS,
    "ConfirmQueueProposalRequestResource.project_id": MAXIMUM_PROJECT_ID_CHARACTERS,
    "ConfirmQueueProposalRequestResource.tracker_item_reference": (
        MAXIMUM_TRACKER_ITEM_REFERENCE_CHARACTERS
    ),
    "ConfirmQueueProposalRequestResource.rationale": (
        MAXIMUM_QUEUE_ADMISSION_RATIONALE_CHARACTERS
    ),
    "PutQueueProposalRequestResource.project_id": MAXIMUM_PROJECT_ID_CHARACTERS,
    "PutQueueProposalRequestResource.tracker_item_reference": (
        MAXIMUM_TRACKER_ITEM_REFERENCE_CHARACTERS
    ),
    "PutQueueProjectPolicyRequestResource.automation_label": (
        MAXIMUM_QUEUE_AUTOMATION_LABEL_CHARACTERS
    ),
    "QueueAdmissionResource.rationale": (MAXIMUM_QUEUE_ADMISSION_RATIONALE_CHARACTERS),
    "QueueItemResource.project_id": MAXIMUM_PROJECT_ID_CHARACTERS,
    "QueueItemResource.tracker_item_reference": (
        MAXIMUM_TRACKER_ITEM_REFERENCE_CHARACTERS
    ),
    "QueueItemResource.title": MAXIMUM_QUEUE_ITEM_TITLE_CHARACTERS,
    "QueueProjectPolicyResource.project_id": MAXIMUM_PROJECT_ID_CHARACTERS,
    "QueueProjectPolicyResource.automation_label": (
        MAXIMUM_QUEUE_AUTOMATION_LABEL_CHARACTERS
    ),
    "WorkflowRevisionProvenanceResource.source": (
        MAXIMUM_PUBLIC_DEFINITION_SOURCE_REFERENCE_CHARACTERS
    ),
    "WorkflowRevisionProvenanceResource.source_path": (
        MAXIMUM_REPOSITORY_PATH_CHARACTERS
    ),
    "NodeRefusalOutputResource.value_base64": MAXIMUM_REFUSED_OUTPUT_BASE64_CHARACTERS,
    "AssistantTurnEventResource.text": MAXIMUM_TRANSCRIPT_STEP_CHARACTERS,
    "ToolCalledEventResource.arguments": MAXIMUM_TRANSCRIPT_STEP_CHARACTERS,
    "ToolCalledEventResource.name": MAXIMUM_TRANSCRIPT_STEP_CHARACTERS,
    "ToolReturnedEventResource.name": MAXIMUM_TRANSCRIPT_STEP_CHARACTERS,
    "ToolReturnedEventResource.result": MAXIMUM_TRANSCRIPT_STEP_CHARACTERS,
    "ProviderTerminalRefusalEventResource.terminal_reason": (
        MAXIMUM_TRANSCRIPT_STEP_CHARACTERS
    ),
    "ProviderTerminalRefusalEventResource.api_error_status": (
        MAXIMUM_TRANSCRIPT_STEP_CHARACTERS
    ),
    "ProviderTerminalRefusalEventResource.text": MAXIMUM_TRANSCRIPT_STEP_CHARACTERS,
    "UnrecognisedProviderOutputEventResource.text": MAXIMUM_TRANSCRIPT_STEP_CHARACTERS,
    "DefectiveRunRowResource.detail": MAXIMUM_RUN_ROW_DEFECT_DETAIL_CHARACTERS,
}


def _wire_models(module: ModuleType) -> Iterator[type[BaseModel]]:
    for name, member in vars(module).items():
        if (
            isinstance(member, type)
            and issubclass(member, BaseModel)
            and member.__module__ == module.__name__
            and member.__name__ == name
        ):
            yield member


def _declared_wire_bounds() -> Mapping[str, int]:
    """Every maximum length the wire actually enforces, read off the models."""
    declared: dict[str, int] = {}
    for module in WIRE_MODULES:
        for model in _wire_models(module):
            for field_name, field in model.model_fields.items():
                for constraint in field.metadata:
                    if isinstance(constraint, MaxLen):
                        declared[f"{model.__name__}.{field_name}"] = (
                            constraint.max_length
                        )
    return declared


def _names_this_module_binds(tree: ast.Module) -> frozenset[str]:
    """Every name the source assigns to itself, wherever it assigns it.

    Scope is deliberately ignored. A bound that reads a name this file wrote is
    not derived from the contract no matter which body the writing happened in,
    and every `max_length=` in a wire module sits inside a class body -- so a
    module-level-only reading was blind exactly where the bounds live.

    Both assignment forms count. `_WIRE_BOUND = 1_024` and
    `_WIRE_BOUND: int = 1_024` differ only in whether an annotation is present,
    which is nothing the owner cares about.
    """

    bound: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            bound.update(
                target.id for target in node.targets if isinstance(target, ast.Name)
            )
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            bound.add(node.target.id)
    return frozenset(bound)


def _reference_root(bound: ast.expr) -> str | None:
    """The name a bound expression ultimately reads, or nothing if it computes one.

    `MAXIMUM_AGENT_FIELD_CHARACTERS` and `agents.MAXIMUM_AGENT_FIELD_CHARACTERS`
    both read an owner and answer with the name the reading starts from; a
    literal or an arithmetic expression reads nothing and answers with nothing.
    """

    if isinstance(bound, ast.Name):
        return bound.id
    if isinstance(bound, ast.Attribute):
        return _reference_root(bound.value)
    return None


def unowned_bounds(source: str, where: str) -> tuple[str, ...]:
    """Where this source writes a maximum length it does not take from an owner.

    A bound follows its owner only if it *names* one. Three shapes do not, and
    they fail the same way -- the value is right today and stops moving with the
    contract tomorrow:

      - a literal, which was never connected to anything;
      - an expression, which recomputes the number instead of reading it;
      - a name this module assigned itself, which is a second owner wearing the
        first one's value.

    The third is why comparing values was not enough. `WIRE_BOUND = 1_024` beside
    `max_length=WIRE_BOUND` reports the same integer as the contract and passes
    every check that only asks what the number is.
    """

    tree = ast.parse(source)
    assigned_here = _names_this_module_binds(tree)
    unowned: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for keyword in node.keywords:
            if keyword.arg != "max_length":
                continue
            bound = keyword.value
            root = _reference_root(bound)
            if root is None or root in assigned_here:
                unowned.append(f"{where}:{bound.lineno}")
    return tuple(unowned)


def _unowned_wire_bounds(module: ModuleType) -> tuple[str, ...]:
    source = Path(inspect.getfile(module)).read_text(encoding="utf-8")
    return unowned_bounds(source, module.__name__)


def test_every_bounded_wire_field_is_bounded_by_the_contract_that_owns_it() -> None:
    """Drift in both directions is red: a moved value, or an unowned new field."""
    assert _declared_wire_bounds() == OWNED_WIRE_BOUNDS


@pytest.mark.proves("a-persisted-bound-is-written-once-and-derived-everywhere")
def test_no_wire_field_writes_a_bound_it_does_not_take_from_its_owner() -> None:
    """Every bound on the wire names the contract it follows, and nothing else."""
    unowned = tuple(
        location for module in WIRE_MODULES for location in _unowned_wire_bounds(module)
    )

    assert unowned == ()


@pytest.mark.parametrize("model_id", [" opus", "opus ", "opus mini", "opus\nmini"])
def test_model_registry_input_refuses_non_exact_model_ids(model_id: str) -> None:
    with pytest.raises(ValidationError):
        requests.PutModelRegistryRevisionRequestResource(
            revision_number=1,
            entries=(
                requests.ModelRegistryEntryInputResource(
                    model_id=model_id,
                    agent_configuration_revision_hash="a" * 64,
                ),
            ),
        )


@pytest.mark.parametrize(
    ("shape", "source", "reported"),
    [
        ("the owner itself", "Field(max_length=MAXIMUM_AGENT_FIELD_CHARACTERS)", False),
        ("a literal", "Field(max_length=1_024)", True),
        ("an expression over the owner", "Field(max_length=512 * 2)", True),
        (
            "a name this module assigned itself",
            "WIRE_BOUND = 1_024\nField(max_length=WIRE_BOUND)",
            True,
        ),
        (
            "a local alias of the owner",
            "WIRE_BOUND = MAXIMUM_AGENT_FIELD_CHARACTERS\nField(max_length=WIRE_BOUND)",
            True,
        ),
        # Where the bounds actually live. Every max_length in a wire module sits
        # inside a class body, so a guard that read only module level was blind
        # in the one place it had to see.
        (
            "a name assigned in the class body that holds the field",
            (
                "class R(ApiModel):\n"
                "    WIRE_BOUND = 1_024\n"
                "    role: str = Field(max_length=WIRE_BOUND)\n"
            ),
            True,
        ),
        (
            "a class-body alias of the owner",
            (
                "class R(ApiModel):\n"
                "    WIRE_BOUND = MAXIMUM_AGENT_FIELD_CHARACTERS\n"
                "    role: str = Field(max_length=WIRE_BOUND)\n"
            ),
            True,
        ),
        (
            "an annotated assignment, which is an assignment with a type on it",
            "WIRE_BOUND: int = 1_024\nField(max_length=WIRE_BOUND)",
            True,
        ),
        (
            "an annotated assignment in the class body",
            (
                "class R(ApiModel):\n"
                "    WIRE_BOUND: int = 1_024\n"
                "    role: str = Field(max_length=WIRE_BOUND)\n"
            ),
            True,
        ),
        # Reading the owner through the module that holds it is still reading the
        # owner, so the guard must not refuse it and call that strictness.
        (
            "the owner reached through its own module",
            "Field(max_length=agents.MAXIMUM_AGENT_FIELD_CHARACTERS)",
            False,
        ),
        (
            "the owner inside a class body",
            (
                "class R(ApiModel):\n"
                "    role: str = Field(max_length=MAXIMUM_AGENT_FIELD_CHARACTERS)\n"
            ),
            False,
        ),
    ],
)
@pytest.mark.proves("a-persisted-bound-is-written-once-and-derived-everywhere")
def test_the_guard_reports_every_bound_that_stopped_naming_its_owner(
    shape: str, source: str, reported: bool
) -> None:
    """Every shape that keeps the value and loses the derivation, pinned.

    The class-body and annotated rows are here because the first version of this
    guard passed them: it read module-level `ast.Assign` only, and every bound on
    the wire is written inside a class body. A guard that cannot fail where the
    subject lives proves its fixture, not its reach -- so the reach is the thing
    pinned, including the two readings that must stay green.

    Pinned against synthetic source rather than by editing a wire module, so the
    guard's own reach is a standing claim instead of something a reviewer has to
    re-prove by hand each time. The last two are the pair that passed both earlier
    checks: same value, no owner.
    """
    assert bool(unowned_bounds(source, shape)) is reported
