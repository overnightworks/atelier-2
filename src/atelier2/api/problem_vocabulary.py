from __future__ import annotations

from dataclasses import dataclass

from atelier2.contracts.adapter_operations_v3 import AdapterOperationRefusal
from atelier2.contracts.agent_definitions import AgentDefinitionRefusal
from atelier2.contracts.artifacts import ArtifactRefusal
from atelier2.contracts.budgets_v3 import BudgetRevisionRefusal
from atelier2.contracts.schemas_v3 import SchemaDocumentRefusal
from atelier2.contracts.tool_grants_v3 import ToolGrantRefusal

ROUTE_NOT_FOUND_ACTION = "Use a path described by this API's OpenAPI document"
"""What a caller who guessed a path is told to read instead.

A consumer holding nothing but a base URL meets this refusal first, so it is
where the API says where its own description lives. The sentence is written
once; where the document is served is the application's decision, and
`route_not_found_detail` adds it rather than restating it here.
"""


@dataclass(frozen=True)
class ProblemDefinition:
    status: int
    title: str
    detail: str


def artifact_problem_code(refusal: ArtifactRefusal) -> str:
    """The problem code one artifact refusal becomes on the wire.

    The contract already names each fault, and a caller whose material was
    turned away reads which of the two it was from the type.
    """

    return refusal.value


def _artifact_problems() -> dict[str, ProblemDefinition]:
    return {
        artifact_problem_code(refusal): ProblemDefinition(
            422,
            "Artifact refused",
            f"The bytes are not material this store keeps ({refusal.value}).",
        )
        for refusal in ArtifactRefusal
    }


ARTIFACT_PROBLEM_CODES = tuple(
    artifact_problem_code(refusal) for refusal in ArtifactRefusal
)


def schema_document_problem_code(refusal: SchemaDocumentRefusal) -> str:
    """The problem code one schema-profile refusal becomes on the wire.

    The profile already names each fault. The route must not collapse those
    names into `invalid-request`: the caller reads the reason from the type.
    """

    return f"schema-{refusal.value}"


def _schema_document_problems() -> dict[str, ProblemDefinition]:
    return {
        schema_document_problem_code(refusal): ProblemDefinition(
            422,
            "Invalid schema document",
            f"The document is not a schema this product enforces ({refusal.value}).",
        )
        for refusal in SchemaDocumentRefusal
    }


SCHEMA_DOCUMENT_PROBLEM_CODES = tuple(
    schema_document_problem_code(refusal) for refusal in SchemaDocumentRefusal
)


def budget_document_problem_code(refusal: BudgetRevisionRefusal) -> str:
    """The problem code one budget-content refusal becomes on the wire.

    The content owner already names each fault, and an author fixing a budget
    needs to read which bound was wrong from the type, not from prose.
    """

    return f"budget-{refusal.value}"


def _budget_document_problems() -> dict[str, ProblemDefinition]:
    return {
        budget_document_problem_code(refusal): ProblemDefinition(
            422,
            "Invalid budget document",
            f"The document bounds no attempt this runtime runs ({refusal.value}).",
        )
        for refusal in BudgetRevisionRefusal
    }


BUDGET_DOCUMENT_PROBLEM_CODES = tuple(
    budget_document_problem_code(refusal) for refusal in BudgetRevisionRefusal
)


def tool_grant_document_problem_code(refusal: ToolGrantRefusal) -> str:
    """The problem code one grant-document refusal becomes on the wire.

    The contract names each fault in the underscored resolution vocabulary.
    The publication door speaks the hyphenated problem vocabulary the rest of
    this API already uses, so the author reads the same shape as a schema or
    budget refusal. The contract token itself still stands in the detail.
    """

    return f"tool-{refusal.value.replace('_', '-')}"


def _tool_grant_document_problems() -> dict[str, ProblemDefinition]:
    return {
        tool_grant_document_problem_code(refusal): ProblemDefinition(
            422,
            "Invalid tool grant document",
            f"The document is not a tool grant this runtime redeems ({refusal.value}).",
        )
        for refusal in ToolGrantRefusal
    }


TOOL_GRANT_DOCUMENT_PROBLEM_CODES = tuple(
    tool_grant_document_problem_code(refusal) for refusal in ToolGrantRefusal
)


def adapter_operation_document_problem_code(
    refusal: AdapterOperationRefusal,
) -> str:
    """The problem code one adapter-operation refusal becomes on the wire.

    The contract names each fault in the underscored resolution vocabulary.
    The publication door speaks the hyphenated problem vocabulary the rest of
    this API already uses, so the author reads the same shape as a schema or
    tool-grant refusal. The contract token itself still stands in the detail.
    """

    return f"adapter-operation-{refusal.value.replace('_', '-')}"


def _adapter_operation_document_problems() -> dict[str, ProblemDefinition]:
    return {
        adapter_operation_document_problem_code(refusal): ProblemDefinition(
            422,
            "Invalid adapter operation document",
            "The document is not an adapter operation this runtime performs "
            f"({refusal.value}).",
        )
        for refusal in AdapterOperationRefusal
    }


ADAPTER_OPERATION_DOCUMENT_PROBLEM_CODES = tuple(
    adapter_operation_document_problem_code(refusal)
    for refusal in AdapterOperationRefusal
)


def agent_definition_document_problem_code(refusal: AgentDefinitionRefusal) -> str:
    """The problem code one agent-definition refusal becomes on the wire.

    The authoring contract already names each fault in the hyphenated problem
    vocabulary, so the author reads the same shape as a schema or tool-grant
    refusal. The contract token itself still stands in the detail.
    """

    return f"agent-definition-{refusal.value}"


def _agent_definition_document_problems() -> dict[str, ProblemDefinition]:
    return {
        agent_definition_document_problem_code(refusal): ProblemDefinition(
            422,
            "Invalid agent definition document",
            "The document is not an agent definition this catalog publishes "
            f"({refusal.value}).",
        )
        for refusal in AgentDefinitionRefusal
    }


AGENT_DEFINITION_DOCUMENT_PROBLEM_CODES = tuple(
    agent_definition_document_problem_code(refusal)
    for refusal in AgentDefinitionRefusal
)


PROBLEM_DEFINITIONS: dict[str, ProblemDefinition] = {
    "auth-profile-revision-conflict": ProblemDefinition(
        409,
        "Auth profile revision conflict",
        "Use a new revision_number or retry the exact original auth profile revision.",
    ),
    "auth-profile-revision-collision": ProblemDefinition(
        409,
        "Auth profile revision collision",
        "Stop mutation and inspect durable auth profile revision integrity.",
    ),
    "auth-profile-revision-not-found": ProblemDefinition(
        404,
        "Auth profile revision not found",
        "Publish the exact auth profile revision before publishing an agent configuration.",
    ),
    "agent-executor-binding-unavailable": ProblemDefinition(
        409,
        "Agent executor binding unavailable",
        "Register the exact provider and executor revision before publishing or starting this configuration.",
    ),
    "agent-configuration-revision-collision": ProblemDefinition(
        409,
        "Agent configuration revision collision",
        "Stop mutation and inspect durable agent configuration revision integrity.",
    ),
    "agent-configuration-revision-not-found": ProblemDefinition(
        404,
        "Agent configuration revision not found",
        "Publish every exact agent configuration revision before starting the run.",
    ),
    "invalid-agent-bindings": ProblemDefinition(
        422,
        "Invalid agent bindings",
        "Bind every workflow agent role exactly once and no other role.",
    ),
    "uncast-agent-roles": ProblemDefinition(
        422,
        "Agent roles need models",
        "Choose a registered model for every workflow role without one.",
    ),
    "binding-constraint-refused": ProblemDefinition(
        422,
        "Binding constraint refused",
        "Bind the constrained nodes to different agent configuration revisions. The constraint checks occupation, not independent judgment.",
    ),
    "invalid-agent-attempt-id": ProblemDefinition(
        400,
        "Invalid agent attempt id",
        "Use exactly 64 lowercase hexadecimal characters.",
    ),
    "agent-attempt-not-found": ProblemDefinition(
        404,
        "Agent attempt not found",
        "Cancel an attempt that belongs to the referenced run.",
    ),
    "agent-attempt-not-current": ProblemDefinition(
        409,
        "Agent attempt is not current",
        "Reload the run and cancel only its current attempt.",
    ),
    "agent-attempt-cancellation-stale": ProblemDefinition(
        409,
        "Agent attempt cancellation is stale",
        "Reload the run and bind the command to its current attempt state version.",
    ),
    "agent-attempt-terminal": ProblemDefinition(
        409,
        "Agent attempt is terminal",
        "A completed attempt can no longer be cancelled.",
    ),
    "cancellation-command-conflict": ProblemDefinition(
        409,
        "Cancellation command conflict",
        "Use a new command_id or retry the exact original cancellation command.",
    ),
    "replacement-not-allowed": ProblemDefinition(
        409,
        "Replacement is not allowed",
        "Only ordinal one may request the single replacement attempt.",
    ),
    "invalid-public-run-reference": ProblemDefinition(
        400, "Invalid public run reference", "Use a canonical run1 public reference."
    ),
    "invalid-public-project-reference": ProblemDefinition(
        400,
        "Invalid public project reference",
        "Use a canonical project1 public reference.",
    ),
    "invalid-event-cursor": ProblemDefinition(
        400,
        "Invalid event cursor",
        "Use a canonical event1 cursor returned by this API.",
    ),
    "invalid-revision-hash": ProblemDefinition(
        400, "Invalid revision hash", "Use exactly 64 lowercase hexadecimal characters."
    ),
    "event-cursor-run-mismatch": ProblemDefinition(
        409,
        "Event cursor belongs to another run",
        "Reconnect with a cursor returned for this run.",
    ),
    "event-cursor-ahead": ProblemDefinition(
        409,
        "Event cursor is ahead of durable history",
        "Reconnect from a cursor at or below the durable head.",
    ),
    "invalid-request": ProblemDefinition(
        422, "Invalid request", "Correct the request fields and submit it again."
    ),
    "invalid-base64": ProblemDefinition(
        422, "Invalid base64", "Use canonical RFC 4648 base64 with required padding."
    ),
    "invalid-workflow-document": ProblemDefinition(
        422,
        "Invalid workflow document",
        "Submit exact bytes for a safe closed workflow graph.",
    ),
    **_artifact_problems(),
    "invalid-artifact-hash": ProblemDefinition(
        400,
        "Invalid artifact hash",
        "Name an artifact by the exact address its publication answered.",
    ),
    "artifact-not-found": ProblemDefinition(
        404,
        "Artifact not found",
        "Publish the exact bytes before reading them back under their address.",
    ),
    **_schema_document_problems(),
    "schema-revision-collision": ProblemDefinition(
        409,
        "Schema revision collision",
        "Stop and inspect durable schema revision integrity.",
    ),
    "schema-revision-not-found": ProblemDefinition(
        404,
        "Schema revision not found",
        "Publish the exact schema revision before reading its bytes.",
    ),
    **_budget_document_problems(),
    "budget-revision-collision": ProblemDefinition(
        409,
        "Budget revision collision",
        "Stop and inspect durable budget revision integrity.",
    ),
    **_tool_grant_document_problems(),
    "tool-grant-revision-collision": ProblemDefinition(
        409,
        "Tool grant revision collision",
        "Stop and inspect durable tool grant revision integrity.",
    ),
    **_adapter_operation_document_problems(),
    "adapter-operation-revision-collision": ProblemDefinition(
        409,
        "Adapter operation revision collision",
        "Stop and inspect durable adapter operation revision integrity.",
    ),
    **_agent_definition_document_problems(),
    "agent-definition-revision-collision": ProblemDefinition(
        409,
        "Agent definition revision collision",
        "Stop and inspect durable agent definition revision integrity.",
    ),
    "agent-definition-revision-not-found": ProblemDefinition(
        404,
        "Agent definition revision not found",
        "Publish the exact agent definition revision before reading its fields.",
    ),
    "library-document-ambiguous": ProblemDefinition(
        422,
        "Document matches more than one library kind",
        "Rename or edit the document so exactly one kind's marker claims it.",
    ),
    "unsupported-media-type": ProblemDefinition(
        415,
        "Unsupported media type",
        "Use the media type documented for this operation.",
    ),
    "not-acceptable": ProblemDefinition(
        406, "Not acceptable", "Accept text/event-stream or */*."
    ),
    "catalog-revision-unpublished": ProblemDefinition(
        409,
        "Catalog revision is unpublished",
        "Publish the revision through the door of its kind before giving it a name.",
    ),
    "catalog-name-held": ProblemDefinition(
        409,
        "Catalog name is held",
        "Another lineage of this kind already holds that name.",
    ),
    "catalog-revision-owned": ProblemDefinition(
        409,
        "Catalog revision is owned",
        "That revision already belongs to another lineage.",
    ),
    "project-unknown": ProblemDefinition(
        404,
        "Project unknown",
        "Use a project id this installation has configured.",
    ),
    "model-registry-missing": ProblemDefinition(
        404,
        "Model registry not found",
        "Publish a model-registry revision for this provider.",
    ),
    "model-registry-revision-conflict": ProblemDefinition(
        409,
        "Model registry revision conflict",
        "Use a new revision_number or retry the exact original registry revision.",
    ),
    "model-registry-revision-collision": ProblemDefinition(
        409,
        "Model registry revision collision",
        "Stop mutation and inspect durable model-registry integrity.",
    ),
    "project-model-defaults-missing": ProblemDefinition(
        404,
        "Project model defaults not found",
        "Choose the project's model defaults for difficulty 1, 2, and 3.",
    ),
    "project-model-defaults-revision-conflict": ProblemDefinition(
        409,
        "Project model defaults revision conflict",
        "Use a new revision_number or retry the exact original defaults revision.",
    ),
    "project-model-defaults-revision-collision": ProblemDefinition(
        409,
        "Project model defaults revision collision",
        "Stop mutation and inspect durable project model-default integrity.",
    ),
    "catalog-lineage-missing": ProblemDefinition(
        404,
        "Catalog lineage not found",
        "No lineage of this kind carries that id.",
    ),
    "catalog-name-not-found": ProblemDefinition(
        404,
        "Catalog name not found",
        "No lineage of this kind holds that name at that position.",
    ),
    "catalog-lineage-retired": ProblemDefinition(
        410,
        "Catalog lineage retired",
        "This name was retired; it resolves to no revision a run may use.",
    ),
    "catalog-revision-not-a-member": ProblemDefinition(
        409,
        "Catalog revision is not a member",
        "The name resolved to a revision its lineage does not admit.",
    ),
    "invalid-catalog-position": ProblemDefinition(
        400,
        "Invalid catalog position",
        "Ask for head or an exact positive member number.",
    ),
    "workflow-revision-not-found": ProblemDefinition(
        404,
        "Workflow revision not found",
        "Publish the exact workflow revision before starting a run.",
    ),
    "run-not-found": ProblemDefinition(
        404, "Run not found", "Use a public reference for a durable run that exists."
    ),
    "node-not-found": ProblemDefinition(
        404, "Node not found", "Answer a node in the referenced workflow graph."
    ),
    "revision-collision": ProblemDefinition(
        409,
        "Workflow revision collision",
        "Stop and inspect durable revision integrity.",
    ),
    "workflow-format-not-executable": ProblemDefinition(
        409,
        "Workflow format is not executable",
        "This revision is published and no runtime here runs its format version yet.",
    ),
    "run-input-refused": ProblemDefinition(
        422,
        "Run input refused",
        "Supply exactly the orders this workflow declares, each satisfying the "
        "schema its author pinned.",
    ),
    "run-identity-conflict": ProblemDefinition(
        409,
        "Run identity conflict",
        "Use a new run_id or retry the exact original revision.",
    ),
    "run-fork-origin-not-terminal": ProblemDefinition(
        409,
        "Run fork origin is not terminal",
        "Fork only a completed, failed, or cancelled run.",
    ),
    "run-fork-node-missing": ProblemDefinition(
        409,
        "Run fork node is missing",
        "Restart from a node in the origin's bound workflow revision.",
    ),
    "run-fork-loop-unsupported": ProblemDefinition(
        409,
        "Run fork loop is unsupported",
        "This API version forks only a linear workflow without loop rounds.",
    ),
    "run-fork-prefix-not-reusable": ProblemDefinition(
        409,
        "Run fork prefix is not reusable",
        "Restart at or before the first predecessor without a verified success fact.",
    ),
    "run-fork-command-conflict": ProblemDefinition(
        409,
        "Run fork command conflict",
        "Use a new idempotency_key or retry the exact original fork target.",
    ),
    "answer-revision-conflict": ProblemDefinition(
        409, "Answer revision conflict", "Retry with the run's exact workflow revision."
    ),
    "answer-state-conflict": ProblemDefinition(
        409,
        "Answer state conflict",
        "Answer only the run's current waiting input node.",
    ),
    "answer-execution-stale": ProblemDefinition(
        409,
        "Answer execution is stale",
        "Reload the run and answer only its current waiting execution.",
    ),
    "reconciliation-target-missing": ProblemDefinition(
        409,
        "Reconciliation target missing",
        "Reconcile only the run's current unresolved Action.",
    ),
    "reconciliation-stale": ProblemDefinition(
        409,
        "Reconciliation is stale",
        "Reload the run and bind a command to its current intent state version.",
    ),
    "reconciliation-command-conflict": ProblemDefinition(
        409,
        "Reconciliation command conflict",
        "Use a new command_id or retry the exact original command.",
    ),
    "reconciliation-determination-conflict": ProblemDefinition(
        409,
        "Reconciliation determination conflict",
        "Use a new command_id for a changed determination.",
    ),
    "reconciliation-rejected": ProblemDefinition(
        409,
        "Reconciliation was rejected",
        "Reload the run before issuing another accountable command.",
    ),
    "run-not-cancellable": ProblemDefinition(
        409,
        "Run is not cancellable",
        "Reload the run to see where it stands; no live agent this cancel could "
        "stop is running.",
    ),
    "run-cancellation-command-conflict": ProblemDefinition(
        409,
        "Run cancellation command conflict",
        "Use a new idempotency_key or retry the exact original run-cancel command.",
    ),
    "run-cancellation-overtaken-by-success": ProblemDefinition(
        409,
        "Run cancellation overtaken by success",
        "The agent finished before this cancel reached it; its result stands and "
        "the run moved on.",
    ),
    "project-source-not-connected": ProblemDefinition(
        409,
        "Project source not connected",
        "Serve a project whose source is connected with `atelier2 connect` "
        "before reading its items.",
    ),
    "project-source-already-connected": ProblemDefinition(
        409,
        "Project source already connected",
        "Disconnect the existing source before connecting another one.",
    ),
    "project-source-unknown": ProblemDefinition(
        404,
        "Project source unknown",
        "Reload the project's sources and use a listed source reference.",
    ),
    "project-source-disconnected": ProblemDefinition(
        409,
        "Project source disconnected",
        "Reconnect the source before rotating its token.",
    ),
    "project-source-invalid": ProblemDefinition(
        422,
        "Project source invalid",
        "Use a provider source address this installation recognizes.",
    ),
    "project-source-token-refused": ProblemDefinition(
        422,
        "Project source token refused",
        "Use a token the provider accepts for this source.",
    ),
    "project-source-unavailable": ProblemDefinition(
        503,
        "Project source unavailable",
        "The connected platform did not answer; retry after it becomes reachable.",
    ),
    "project-source-payload-malformed": ProblemDefinition(
        502,
        "Project source payload malformed",
        "The connected platform answered with a shape its adapter refuses; "
        "inspect the platform before retrying.",
    ),
    "queue-admission-revision-conflict": ProblemDefinition(
        409,
        "Queue admission revision conflict",
        "Reload the queue item and admit it against the revision it now holds.",
    ),
    "queue-admission-already-decided": ProblemDefinition(
        409,
        "Queue item is already admitted",
        "This item is already admitted under a different workflow binding or reason.",
    ),
    "queue-admission-authority-refused": ProblemDefinition(
        409,
        "Queue admission authority refused",
        "The proposal does not authorize admission by this decision authority.",
    ),
    "queue-admission-proposal-required": ProblemDefinition(
        409,
        "Queue admission requires a proposal",
        "Propose the queue item before confirming its admission.",
    ),
    "queue-policy-revision-conflict": ProblemDefinition(
        409,
        "Queue policy revision conflict",
        "Reload the project queue policy and replace its next revision.",
    ),
    "queue-proposal-revision-conflict": ProblemDefinition(
        409,
        "Queue proposal revision conflict",
        "Reload the queue item and propose against the revision inspected.",
    ),
    "queue-proposal-already-decided": ProblemDefinition(
        409,
        "Queue proposal already decided",
        "An admitted or differently proposed item cannot be silently replanned.",
    ),
    "queue-proposal-refused": ProblemDefinition(
        422,
        "Queue proposal refused",
        "Use existing project-local prerequisites without a dependency cycle.",
    ),
    "route-not-found": ProblemDefinition(
        404, "Route not found", ROUTE_NOT_FOUND_ACTION + "."
    ),
    "invalid-public-source-reference": ProblemDefinition(
        400,
        "Invalid public source reference",
        "Use the source reference returned by this API.",
    ),
    "method-not-allowed": ProblemDefinition(
        405, "Method not allowed", "Use the HTTP method described for this path."
    ),
    "temporarily-unavailable": ProblemDefinition(
        503,
        "Temporarily unavailable",
        "Retry after the durable store becomes available.",
    ),
    "durable-projection-unrepresentable": ProblemDefinition(
        500,
        "Durable projection cannot be represented",
        "Inspect the durable projection before retrying.",
    ),
    "durable-state-corrupt": ProblemDefinition(
        500, "Durable state is corrupt", "Stop mutation and inspect the durable store."
    ),
    "internal-error": ProblemDefinition(
        500, "Internal error", "Retry only after the server fault has been inspected."
    ),
}
