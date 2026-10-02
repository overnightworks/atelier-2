"""The schemas the API accepts: publications, run starts, answers, commands."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import Field, SecretStr

from atelier2.api.references import (
    MAX_SIGNED_INT64,
    MAXIMUM_RUN_AGENT_BINDINGS,
    REVISION_HASH_PATTERN,
    SHA256_HASH_PATTERN,
)
from atelier2.api.wire.resources import (
    ApiModel,
    ReconciliationDeterminationResource,
)
from atelier2.contracts.agents import (
    MAXIMUM_AGENT_FIELD_CHARACTERS,
    MAXIMUM_PROVIDER_ID_CHARACTERS,
    PROVIDER_ID_PATTERN,
)
from atelier2.contracts.catalog_v3 import (
    CATALOG_ACTIVATED_AT_PATTERN,
    MAXIMUM_CATALOG_ACTOR_CHARACTERS,
    MAXIMUM_LINEAGE_DISPLAY_NAME_CHARACTERS,
)
from atelier2.contracts.host_configuration import (
    EXACT_MODEL_ID_PATTERN,
    MAXIMUM_EXACT_MODEL_ID_CHARACTERS,
    MAXIMUM_MODEL_REGISTRY_ENTRIES,
    MAXIMUM_PROJECT_ID_CHARACTERS,
    MAXIMUM_PROJECT_MODEL_DEFAULTS,
    MAXIMUM_SOURCE_ADDRESS_CHARACTERS,
    MAXIMUM_SOURCE_TOKEN_CHARACTERS,
)
from atelier2.contracts.queue_projection import (
    MAXIMUM_QUEUE_ACTIVE_RUNS,
    MAXIMUM_QUEUE_ADMISSION_RATIONALE_CHARACTERS,
    MAXIMUM_QUEUE_AUTOMATION_LABEL_CHARACTERS,
    MAXIMUM_TRACKER_ITEM_REFERENCE_CHARACTERS,
)
from atelier2.contracts.revisions_v3 import RevisionKind


class RevisionListingView(StrEnum):
    """Which representation of a revision listing the caller asked for.

    Two representations exist because they cost differently: the summary reads
    one column per revision, while the described one reads and parses every
    document it lists. Making the caller name the one it needs keeps the cheaper
    answer the default and keeps both of them reachable, rather than serving one
    shape the document still advertises the other for.
    """

    SUMMARY = "summary"
    DESCRIBED = "described"


class FoundCatalogLineageRequestResource(ApiModel):
    """Found a lineage for one published revision of one kind.

    The kind travels beside the hash because a hash alone names no publication:
    the same bytes may be published under more than one kind, and the lineage
    the catalog founds belongs to exactly one of them.

    A format that authors its own name -- a V3 workflow, an agent definition --
    supplies it from its bytes; a format that authors none requires the explicit
    name.
    """

    # A JSON body spells the kind as its own value; strict decoding alone would
    # demand an enum instance no wire can carry.
    kind: RevisionKind = Field(strict=False)
    catalog_revision_hash: str = Field(pattern=SHA256_HASH_PATTERN)
    display_name: str | None = Field(
        default=None,
        min_length=1,
        max_length=MAXIMUM_LINEAGE_DISPLAY_NAME_CHARACTERS,
    )
    actor: str = Field(min_length=1, max_length=MAXIMUM_CATALOG_ACTOR_CHARACTERS)
    activated_at: str = Field(pattern=CATALOG_ACTIVATED_AT_PATTERN)


class AdmitCatalogMemberRequestResource(ApiModel):
    """Admit one published revision into the lineage named by the path.

    It carries no name: the lineage already holds one, and letting a caller
    state it here would make an admission a rename. It does carry the kind, for
    the same reason founding does -- the hash it names is a publication only
    under one.
    """

    kind: RevisionKind = Field(strict=False)
    catalog_revision_hash: str = Field(pattern=SHA256_HASH_PATTERN)
    actor: str = Field(min_length=1, max_length=MAXIMUM_CATALOG_ACTOR_CHARACTERS)
    activated_at: str = Field(pattern=CATALOG_ACTIVATED_AT_PATTERN)


class RetireCatalogLineageRequestResource(ApiModel):
    """Attribute the event that removes one lineage from the live catalog."""

    actor: str = Field(min_length=1, max_length=MAXIMUM_CATALOG_ACTOR_CHARACTERS)
    activated_at: str = Field(pattern=CATALOG_ACTIVATED_AT_PATTERN)


class ConfirmQueueProposalRequestResource(ApiModel):
    """Confirm the exact proposal revision the operator inspected."""

    project_id: str = Field(min_length=1, max_length=MAXIMUM_PROJECT_ID_CHARACTERS)
    tracker_item_reference: str = Field(
        min_length=1, max_length=MAXIMUM_TRACKER_ITEM_REFERENCE_CHARACTERS
    )
    rationale: str = Field(
        min_length=1, max_length=MAXIMUM_QUEUE_ADMISSION_RATIONALE_CHARACTERS
    )
    expected_revision: int = Field(ge=0, le=MAX_SIGNED_INT64)


class QueuePriorityRankRequestResource(ApiModel):
    rank: int = Field(ge=1, le=MAX_SIGNED_INT64)


class PutQueueProposalRequestResource(ApiModel):
    project_id: str = Field(min_length=1, max_length=MAXIMUM_PROJECT_ID_CHARACTERS)
    tracker_item_reference: str = Field(
        min_length=1, max_length=MAXIMUM_TRACKER_ITEM_REFERENCE_CHARACTERS
    )
    expected_revision: int = Field(ge=0, le=MAX_SIGNED_INT64)
    priority: QueuePriorityRankRequestResource
    workflow_lineage_id: str = Field(pattern=SHA256_HASH_PATTERN)
    prerequisite_item_ids: tuple[str, ...] = Field(strict=False)
    automation_disposition: Literal["HUMAN_REQUIRED", "AUTOMATION_AUTHORIZED"]
    policy_revision: int | None = Field(default=None, ge=1, le=MAX_SIGNED_INT64)


class PutQueueProjectPolicyRequestResource(ApiModel):
    """The filter, the ceiling, and what a labelled item alone is proposed under.

    The three default fields are stated together or not at all; a policy
    without them admits only items an operator has already proposed.
    """

    revision_number: int = Field(ge=1, le=MAX_SIGNED_INT64)
    expected_revision: int = Field(ge=0, le=MAX_SIGNED_INT64)
    maximum_active_runs: int = Field(ge=1, le=MAXIMUM_QUEUE_ACTIVE_RUNS)
    automation_label: str | None = Field(
        default=None,
        min_length=1,
        max_length=MAXIMUM_QUEUE_AUTOMATION_LABEL_CHARACTERS,
    )
    default_workflow_lineage_id: str | None = Field(
        default=None, pattern=SHA256_HASH_PATTERN
    )
    default_priority_rank: int | None = Field(default=None, ge=1, le=MAX_SIGNED_INT64)
    automation_disposition_default: (
        Literal["HUMAN_REQUIRED", "AUTOMATION_AUTHORIZED"] | None
    ) = None


class PublishAuthProfileRevisionRequestResource(ApiModel):
    profile_id: str = Field(min_length=1, max_length=MAXIMUM_AGENT_FIELD_CHARACTERS)
    revision_number: int = Field(ge=1, le=MAX_SIGNED_INT64)
    provider_id: str = Field(min_length=1, max_length=MAXIMUM_PROVIDER_ID_CHARACTERS)
    auth_mode: Literal["subscription", "api_key"]


class PublishAgentConfigurationRevisionRequestResource(ApiModel):
    model: str = Field(min_length=1, max_length=MAXIMUM_AGENT_FIELD_CHARACTERS)
    auth_profile_revision_hash: str = Field(pattern=SHA256_HASH_PATTERN)
    executor_revision: str = Field(
        min_length=1, max_length=MAXIMUM_AGENT_FIELD_CHARACTERS
    )
    requested_capability: Literal["headless", "headless_with_tools", "interactive"] = (
        "headless"
    )


class StartRunRequestResource(ApiModel):
    run_id: str = Field(min_length=1)
    workflow_revision_hash: str = Field(pattern=REVISION_HASH_PATTERN)


class ConnectProjectSourceRequestResource(ApiModel):
    address: str = Field(min_length=1, max_length=MAXIMUM_SOURCE_ADDRESS_CHARACTERS)
    token: SecretStr = Field(min_length=1, max_length=MAXIMUM_SOURCE_TOKEN_CHARACTERS)


class RotateProjectSourceTokenRequestResource(ApiModel):
    token: SecretStr = Field(min_length=1, max_length=MAXIMUM_SOURCE_TOKEN_CHARACTERS)


class ModelRegistryEntryInputResource(ApiModel):
    model_id: str = Field(
        min_length=1,
        max_length=MAXIMUM_EXACT_MODEL_ID_CHARACTERS,
        pattern=EXACT_MODEL_ID_PATTERN,
    )
    agent_configuration_revision_hash: str = Field(pattern=SHA256_HASH_PATTERN)


class PutModelRegistryRevisionRequestResource(ApiModel):
    revision_number: int = Field(ge=1, le=MAX_SIGNED_INT64)
    entries: tuple[ModelRegistryEntryInputResource, ...] = Field(
        max_length=MAXIMUM_MODEL_REGISTRY_ENTRIES, strict=False
    )


class ValidateModelRegistryEntryRequestResource(ApiModel):
    agent_configuration_revision_hash: str = Field(pattern=SHA256_HASH_PATTERN)


class ProjectModelDefaultInputResource(ApiModel):
    difficulty: Literal[1, 2, 3]
    model_registry_revision_hash: str = Field(pattern=SHA256_HASH_PATTERN)
    provider_id: str = Field(
        min_length=1,
        max_length=MAXIMUM_PROVIDER_ID_CHARACTERS,
        pattern=PROVIDER_ID_PATTERN,
    )
    model_id: str = Field(
        min_length=1,
        max_length=MAXIMUM_EXACT_MODEL_ID_CHARACTERS,
        pattern=EXACT_MODEL_ID_PATTERN,
    )
    agent_configuration_revision_hash: str = Field(pattern=SHA256_HASH_PATTERN)


class PutProjectModelDefaultsRevisionRequestResource(ApiModel):
    revision_number: int = Field(ge=1, le=MAX_SIGNED_INT64)
    defaults: tuple[ProjectModelDefaultInputResource, ...] = Field(
        max_length=MAXIMUM_PROJECT_MODEL_DEFAULTS, strict=False
    )


class ModelResolutionOverrideResource(ApiModel):
    role: str = Field(min_length=1, max_length=MAXIMUM_AGENT_FIELD_CHARACTERS)
    agent_configuration_revision_hash: str = Field(pattern=SHA256_HASH_PATTERN)


class ResolveProjectModelsRequestResource(ApiModel):
    workflow_revision_hash: str = Field(pattern=REVISION_HASH_PATTERN)
    overrides: tuple[ModelResolutionOverrideResource, ...] = Field(
        max_length=MAXIMUM_RUN_AGENT_BINDINGS, strict=False
    )


class StartRunAgentBindingResourceV2(ApiModel):
    role: str = Field(min_length=1, max_length=MAXIMUM_AGENT_FIELD_CHARACTERS)
    agent_configuration_revision_hash: str = Field(pattern=SHA256_HASH_PATTERN)


class StartRunRequestResourceV2(ApiModel):
    workflow_format_version: Literal[2]
    run_id: str = Field(min_length=1)
    workflow_revision_hash: str = Field(pattern=REVISION_HASH_PATTERN)
    agent_bindings: tuple[StartRunAgentBindingResourceV2, ...] = Field(
        max_length=MAXIMUM_RUN_AGENT_BINDINGS, strict=False
    )


class ArtifactOrderResource(ApiModel):
    """One order whose value is an artifact already published: a name and its address.

    It is a second shape rather than a second optional field, so a start cannot
    say both and cannot say neither, and so a reader never has to guess whether
    a string is a value or an address. The address is spelled the way the
    publication answered it, so a caller writes back the field it just read.
    """

    name: str = Field(min_length=1)
    artifact_hash: str = Field(pattern=SHA256_HASH_PATTERN)


class WorkItemOrderResource(ApiModel):
    """One order whose value the start reads from the project's own tracker.

    A third shape for the same reason the second one exists: a caller says
    which item, never what it says. The bytes the run pins are the observed
    revision the start read at that moment (ADR 0010 §5) -- the platform's
    own, not the caller's -- so two starts naming the same item on either side
    of an edit are honestly two different runs.
    """

    name: str = Field(min_length=1)
    work_item: str = Field(
        min_length=1, max_length=MAXIMUM_TRACKER_ITEM_REFERENCE_CHARACTERS
    )


AnyStartRunOrderResource = ArtifactOrderResource | WorkItemOrderResource


class StartRunRequestResourceV3(ApiModel):
    """A start that carries the orders the document declares, beside it."""

    workflow_format_version: Literal[3]
    run_id: str = Field(min_length=1)
    workflow_revision_hash: str = Field(pattern=REVISION_HASH_PATTERN)
    agent_bindings: tuple[StartRunAgentBindingResourceV2, ...] = Field(
        max_length=MAXIMUM_RUN_AGENT_BINDINGS, strict=False
    )
    orders: tuple[AnyStartRunOrderResource, ...] = Field(strict=False)


AnyStartRunRequestResource = (
    StartRunRequestResource | StartRunRequestResourceV2 | StartRunRequestResourceV3
)


class AnswerWaitRequestResource(ApiModel):
    workflow_revision_hash: str = Field(pattern=REVISION_HASH_PATTERN)
    node_id: str = Field(min_length=1)
    expected_node_execution_id: str = Field(pattern=SHA256_HASH_PATTERN)
    actor: Literal["operator"]
    answer_base64: str = Field(min_length=1)


class ReconcileRunRequestResource(ApiModel):
    command_id: str = Field(min_length=1)
    expected_intent_state_version: int = Field(ge=0, le=MAX_SIGNED_INT64)
    actor: str = Field(min_length=1)
    evidence: str = Field(min_length=1)
    determination: ReconciliationDeterminationResource


class CancelAgentAttemptRequestResource(ApiModel):
    command_id: str = Field(min_length=1, max_length=MAXIMUM_AGENT_FIELD_CHARACTERS)
    expected_attempt_state_version: int = Field(ge=0, le=MAX_SIGNED_INT64)
    replacement: Literal["NONE", "ONE"]


class CancelRunRequestResource(ApiModel):
    """One operator's confirmed V3 run-cancel, in the words #439 D2 settled.

    The client sends only the `idempotency_key` it repeats on retry; the durable
    command id is minted server-side into the reserved run-cancel namespace, so a
    command outside it is unrepresentable. `expected_node_execution_id` is D2's
    fence: it binds run, revision, node and declared-loop round in the one value
    the store recomputes rather than trusts, and the client writes back exactly
    the `cancellation.target_node_execution_id` the run resource just served it.
    """

    idempotency_key: str = Field(
        min_length=1, max_length=MAXIMUM_AGENT_FIELD_CHARACTERS
    )
    expected_node_execution_id: str = Field(pattern=SHA256_HASH_PATTERN)


class ForkRunRequestResource(ApiModel):
    """The caller-owned retry key and the node where the successor begins."""

    idempotency_key: str = Field(
        min_length=1, max_length=MAXIMUM_AGENT_FIELD_CHARACTERS
    )
    restart_from_node_id: str = Field(
        min_length=1, max_length=MAXIMUM_AGENT_FIELD_CHARACTERS
    )
