from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from typing import Any

from fastapi import FastAPI
from fastapi.openapi.models import OpenAPI
from fastapi.openapi.utils import get_openapi

from atelier2.api.limits import ApiLimits
from atelier2.api.problem_vocabulary import (
    ADAPTER_OPERATION_DOCUMENT_PROBLEM_CODES,
    AGENT_DEFINITION_DOCUMENT_PROBLEM_CODES,
    ARTIFACT_PROBLEM_CODES,
    BUDGET_DOCUMENT_PROBLEM_CODES,
    PROBLEM_DEFINITIONS,
    SCHEMA_DOCUMENT_PROBLEM_CODES,
    TOOL_GRANT_DOCUMENT_PROBLEM_CODES,
)
from atelier2.api.problems import PROBLEM_TYPE_PREFIX
from atelier2.api.references import (
    EVENT_CURSOR_PATTERN,
    MAXIMUM_INVALID_FIELD_PATH_CHARACTERS,
    MAXIMUM_INVALID_FIELD_REASON_CHARACTERS,
    MAXIMUM_PUBLIC_PROJECT_REFERENCE_CHARACTERS,
    MAXIMUM_PUBLIC_SOURCE_REFERENCE_CHARACTERS,
    PUBLIC_PROJECT_REFERENCE_PATTERN,
    PUBLIC_SOURCE_REFERENCE_PATTERN,
)
from atelier2.api.stream import STREAM_FAILURE_CODES
from atelier2.api.wire.events import (
    ActionCompletedEventResourceV3,
    ActionReconciliationRequiredEventResourceV3,
    ActionReconciliationResolvedEventResourceV3,
    AgentCancelledEventResourceV3,
    AgentCancelRequestedEventResourceV3,
    AgentCompletedEventResourceV3,
    AgentExecutorBindingUnavailableEventResourceV3,
    AgentFailedEventResourceV3,
    AgentInterruptedEventResourceV3,
    WaitAnsweredEventResourceV3,
    WaitCancelledEventResourceV3,
    WaitingInputEventResourceV3,
)
from atelier2.api.wire.resources import (
    RunProjectionCorruptResource,
    StreamFailureResource,
)
from atelier2.contracts.agents import MAXIMUM_AGENT_FIELD_CHARACTERS
from atelier2.contracts.host_configuration import ModelResolutionUncastReason
from atelier2.contracts.workflow_documents import WORKFLOW_DOCUMENT_FORMATS

API_PREFIX = "/atelier/api/v1"
_COMPONENT_REFERENCE = "#/components/schemas/{model}"
_COMPONENT_SCHEMA_PREFIX = "#/components/schemas/"
_EVENT_CURSOR_COMPONENT_REF = "#/components/schemas/EventCursor"
_JSON_SCHEMA_DEFS_KEY = "$defs"
_APPLICATION_JSON = "application/json"
WORKFLOW_DOCUMENT_COMPONENT = "WorkflowDocument"
WORKFLOW_DOCUMENT_GRAMMAR_SCOPE = (
    "The shape one published workflow document must have, derived from the models "
    "the publication reads it against. It decides the form: which keys each node "
    "kind carries, how a control edge and a value handover are written, and which "
    "format versions exist at all. It does not decide what only the whole document "
    "answers -- that every named node, output and graph input resolves, that no "
    "cycle closes, that a join is declared where several edges meet, and whether "
    "this build executes the result. Each of those is refused by its own name when "
    "the document is published."
)
AGENT_DEFINITION_REVISIONS_PATH = API_PREFIX + "/agent-definition-revisions"
WORKFLOW_REVISIONS_PATH = API_PREFIX + "/workflow-revisions"
RUNS_PATH = API_PREFIX + "/runs"
ARTIFACTS_PATH = API_PREFIX + "/artifacts"
ARTIFACT_PATH = ARTIFACTS_PATH + "/{artifact_hash}"
EVENT_PATH = API_PREFIX + "/runs/{public_ref}/events"
ATTENTION_EVENT_PATH = API_PREFIX + "/events"
CANCELLATION_PATH = (
    API_PREFIX + "/runs/{public_ref}/agent-attempts/{attempt_id}/cancellations"
)
RUN_CANCELLATION_PATH = API_PREFIX + "/runs/{public_ref}/cancellations"
RUN_FORK_PATH = API_PREFIX + "/runs/{public_ref}/forks"
SEAT_PATH = API_PREFIX + "/seat"
PROJECTS_PATH = API_PREFIX + "/projects"
PROJECT_PATH = PROJECTS_PATH + "/{public_project_reference}"
PROJECT_SOURCE_CONNECTION_PATH = PROJECT_PATH + "/source-connection"
PROJECT_SOURCES_PATH = PROJECT_PATH + "/sources"
PROJECT_SOURCE_PATH = PROJECT_SOURCES_PATH + "/{public_source_reference}"
PROJECT_SOURCE_TOKEN_PATH = PROJECT_SOURCE_PATH + "/token"
MODEL_REGISTRY_PATH = API_PREFIX + "/model-registries/{provider_id}"
MODEL_REGISTRY_VALIDATIONS_PATH = MODEL_REGISTRY_PATH + "/validations"
PROJECT_MODEL_DEFAULTS_PATH = PROJECT_PATH + "/model-defaults"
PROJECT_MODEL_RESOLUTION_PATH = PROJECT_PATH + "/model-resolution"
QUEUE_ADMISSIONS_PATH = API_PREFIX + "/queue-admissions"
QUEUE_PROPOSALS_PATH = API_PREFIX + "/queue-proposals"
QUEUE_ITEMS_PATH = API_PREFIX + "/queue-items"
PROJECT_QUEUE_POLICY_PATH = PROJECT_PATH + "/queue-policy"
PROJECT_SOURCE_IMPORT_PATH = API_PREFIX + "/project-sources/import"
LIBRARY_RECOGNITIONS_PATH = API_PREFIX + "/library/recognitions"
LIBRARY_ADDITIONS_PATH = API_PREFIX + "/library/additions"
LIBRARY_ADDITION_PATH = LIBRARY_ADDITIONS_PATH + "/{intake_id}"
CATALOG_LINEAGES_PATH = API_PREFIX + "/catalog-lineages"
CATALOG_LINEAGE_MEMBERS_PATH = CATALOG_LINEAGES_PATH + "/{lineage_id}/members"
CATALOG_LINEAGE_RETIREMENTS_PATH = CATALOG_LINEAGES_PATH + "/{lineage_id}/retirements"
CATALOG_REVISION_BY_NAME_PATH = API_PREFIX + "/catalog-revisions/by-name/{kind}/{name}"

EVENT_MODELS_V3 = (
    AgentCompletedEventResourceV3,
    AgentFailedEventResourceV3,
    AgentExecutorBindingUnavailableEventResourceV3,
    AgentCancelRequestedEventResourceV3,
    AgentCancelledEventResourceV3,
    AgentInterruptedEventResourceV3,
    ActionReconciliationRequiredEventResourceV3,
    ActionReconciliationResolvedEventResourceV3,
    ActionCompletedEventResourceV3,
    WaitingInputEventResourceV3,
    WaitAnsweredEventResourceV3,
    WaitCancelledEventResourceV3,
)
OPERATION_PROBLEMS: dict[tuple[str, str], tuple[str, ...]] = {
    (API_PREFIX + "/health", "get"): ("internal-error",),
    (SEAT_PATH, "get"): ("internal-error",),
    (API_PREFIX + "/auth-profile-revisions", "post"): (
        "invalid-request",
        "auth-profile-revision-conflict",
        "auth-profile-revision-collision",
        "unsupported-media-type",
        "temporarily-unavailable",
        "durable-state-corrupt",
        "internal-error",
    ),
    (API_PREFIX + "/auth-profile-revisions", "get"): (
        "invalid-revision-hash",
        "invalid-request",
        "temporarily-unavailable",
        "durable-state-corrupt",
        "internal-error",
    ),
    (API_PREFIX + "/agent-configuration-revisions", "post"): (
        "invalid-request",
        "auth-profile-revision-not-found",
        "agent-executor-binding-unavailable",
        "agent-configuration-revision-collision",
        "unsupported-media-type",
        "temporarily-unavailable",
        "durable-state-corrupt",
        "internal-error",
    ),
    (API_PREFIX + "/agent-configuration-revisions", "get"): (
        "invalid-revision-hash",
        "invalid-request",
        "temporarily-unavailable",
        "durable-state-corrupt",
        "internal-error",
    ),
    (ARTIFACTS_PATH, "post"): (
        *ARTIFACT_PROBLEM_CODES,
        "unsupported-media-type",
        "temporarily-unavailable",
        "durable-state-corrupt",
        "internal-error",
    ),
    (ARTIFACT_PATH, "get"): (
        "invalid-artifact-hash",
        "artifact-not-found",
        "durable-state-corrupt",
        "internal-error",
    ),
    (API_PREFIX + "/schema-revisions", "post"): (
        *SCHEMA_DOCUMENT_PROBLEM_CODES,
        "schema-revision-collision",
        "unsupported-media-type",
        "temporarily-unavailable",
        "durable-state-corrupt",
        "internal-error",
    ),
    (API_PREFIX + "/schema-revisions/{schema_revision_hash}", "get"): (
        "invalid-revision-hash",
        "schema-revision-not-found",
        "internal-error",
    ),
    (API_PREFIX + "/budget-revisions", "post"): (
        *BUDGET_DOCUMENT_PROBLEM_CODES,
        "budget-revision-collision",
        "unsupported-media-type",
        "temporarily-unavailable",
        "durable-state-corrupt",
        "internal-error",
    ),
    (API_PREFIX + "/tool-grant-revisions", "post"): (
        *TOOL_GRANT_DOCUMENT_PROBLEM_CODES,
        "tool-grant-revision-collision",
        "unsupported-media-type",
        "temporarily-unavailable",
        "durable-state-corrupt",
        "internal-error",
    ),
    (API_PREFIX + "/adapter-operation-revisions", "post"): (
        *ADAPTER_OPERATION_DOCUMENT_PROBLEM_CODES,
        "adapter-operation-revision-collision",
        "unsupported-media-type",
        "temporarily-unavailable",
        "durable-state-corrupt",
        "internal-error",
    ),
    (AGENT_DEFINITION_REVISIONS_PATH, "post"): (
        *AGENT_DEFINITION_DOCUMENT_PROBLEM_CODES,
        "agent-definition-revision-collision",
        "unsupported-media-type",
        "temporarily-unavailable",
        "durable-state-corrupt",
        "internal-error",
    ),
    (AGENT_DEFINITION_REVISIONS_PATH, "get"): (
        "invalid-revision-hash",
        "invalid-request",
        "temporarily-unavailable",
        "durable-state-corrupt",
        "internal-error",
    ),
    (
        API_PREFIX + "/agent-definition-revisions/{agent_definition_revision_hash}",
        "get",
    ): (
        "invalid-revision-hash",
        "agent-definition-revision-not-found",
        "temporarily-unavailable",
        "durable-state-corrupt",
        "internal-error",
    ),
    (LIBRARY_RECOGNITIONS_PATH, "post"): (
        "library-document-ambiguous",
        "invalid-request",
        "unsupported-media-type",
        "temporarily-unavailable",
        "internal-error",
    ),
    (LIBRARY_ADDITIONS_PATH, "post"): (
        "invalid-request",
        "unsupported-media-type",
        "temporarily-unavailable",
        "durable-state-corrupt",
        "internal-error",
    ),
    (LIBRARY_ADDITION_PATH, "get"): (
        "invalid-request",
        "temporarily-unavailable",
        "durable-state-corrupt",
        "internal-error",
    ),
    (WORKFLOW_REVISIONS_PATH, "post"): (
        "invalid-workflow-document",
        "revision-collision",
        "unsupported-media-type",
        "temporarily-unavailable",
        "durable-state-corrupt",
        "internal-error",
    ),
    (WORKFLOW_REVISIONS_PATH, "get"): (
        "invalid-revision-hash",
        "invalid-request",
        "durable-projection-unrepresentable",
        "temporarily-unavailable",
        "durable-state-corrupt",
        "internal-error",
    ),
    (CATALOG_LINEAGES_PATH, "post"): (
        "invalid-request",
        "unsupported-media-type",
        "catalog-revision-unpublished",
        "catalog-name-held",
        "catalog-revision-owned",
        "catalog-lineage-retired",
        "durable-projection-unrepresentable",
        "temporarily-unavailable",
        "durable-state-corrupt",
        "internal-error",
    ),
    (CATALOG_LINEAGE_MEMBERS_PATH, "post"): (
        "invalid-request",
        "unsupported-media-type",
        "catalog-revision-unpublished",
        "catalog-name-held",
        "catalog-revision-owned",
        "catalog-lineage-missing",
        "catalog-lineage-retired",
        "durable-projection-unrepresentable",
        "temporarily-unavailable",
        "durable-state-corrupt",
        "internal-error",
    ),
    (CATALOG_LINEAGE_RETIREMENTS_PATH, "post"): (
        "invalid-request",
        "catalog-lineage-missing",
        "temporarily-unavailable",
        "durable-state-corrupt",
        "internal-error",
    ),
    (CATALOG_REVISION_BY_NAME_PATH, "get"): (
        "invalid-request",
        "catalog-name-not-found",
        "catalog-lineage-retired",
        "catalog-revision-not-a-member",
        "invalid-catalog-position",
        "temporarily-unavailable",
        "internal-error",
    ),
    (API_PREFIX + "/workflow-revisions/{workflow_revision_hash}", "get"): (
        "invalid-revision-hash",
        "workflow-revision-not-found",
        "durable-projection-unrepresentable",
        "temporarily-unavailable",
        "durable-state-corrupt",
        "internal-error",
    ),
    (PROJECTS_PATH, "get"): (
        "project-unknown",
        "temporarily-unavailable",
        "durable-state-corrupt",
        "internal-error",
    ),
    (PROJECT_PATH, "get"): (
        "invalid-public-project-reference",
        "project-unknown",
        "temporarily-unavailable",
        "durable-state-corrupt",
        "internal-error",
    ),
    (MODEL_REGISTRY_PATH, "put"): (
        "invalid-request",
        "unsupported-media-type",
        "model-registry-revision-conflict",
        "model-registry-revision-collision",
        "temporarily-unavailable",
        "durable-state-corrupt",
        "internal-error",
    ),
    (MODEL_REGISTRY_PATH, "get"): (
        "invalid-request",
        "model-registry-missing",
        "temporarily-unavailable",
        "durable-state-corrupt",
        "internal-error",
    ),
    (MODEL_REGISTRY_VALIDATIONS_PATH, "post"): (
        "invalid-request",
        "model-registry-revision-conflict",
        "model-registry-revision-collision",
        "unsupported-media-type",
        "temporarily-unavailable",
        "durable-state-corrupt",
        "internal-error",
    ),
    (PROJECT_MODEL_DEFAULTS_PATH, "put"): (
        "invalid-public-project-reference",
        "project-unknown",
        "invalid-request",
        "unsupported-media-type",
        "project-model-defaults-revision-conflict",
        "project-model-defaults-revision-collision",
        "temporarily-unavailable",
        "durable-state-corrupt",
        "internal-error",
    ),
    (PROJECT_MODEL_DEFAULTS_PATH, "get"): (
        "invalid-public-project-reference",
        "project-unknown",
        "project-model-defaults-missing",
        "temporarily-unavailable",
        "durable-state-corrupt",
        "internal-error",
    ),
    (PROJECT_MODEL_RESOLUTION_PATH, "post"): (
        "invalid-public-project-reference",
        "project-unknown",
        "workflow-revision-not-found",
        "workflow-format-not-executable",
        "invalid-agent-bindings",
        "invalid-request",
        "unsupported-media-type",
        "temporarily-unavailable",
        "durable-state-corrupt",
        "internal-error",
    ),
    (PROJECT_SOURCE_CONNECTION_PATH, "get"): (
        "invalid-public-project-reference",
        "project-unknown",
        "project-source-not-connected",
        "temporarily-unavailable",
        "durable-state-corrupt",
        "internal-error",
    ),
    (PROJECT_SOURCES_PATH, "get"): (
        "invalid-public-project-reference",
        "project-unknown",
        "temporarily-unavailable",
        "durable-state-corrupt",
        "internal-error",
    ),
    (PROJECT_SOURCES_PATH, "post"): (
        "invalid-public-project-reference",
        "invalid-request",
        "unsupported-media-type",
        "project-unknown",
        "project-source-already-connected",
        "project-source-invalid",
        "project-source-token-refused",
        "project-source-unavailable",
        "durable-state-corrupt",
        "internal-error",
    ),
    (PROJECT_SOURCE_PATH, "delete"): (
        "invalid-public-project-reference",
        "invalid-public-source-reference",
        "project-unknown",
        "project-source-unknown",
        "project-source-unavailable",
        "durable-state-corrupt",
        "internal-error",
    ),
    (PROJECT_SOURCE_TOKEN_PATH, "put"): (
        "invalid-public-project-reference",
        "invalid-public-source-reference",
        "invalid-request",
        "unsupported-media-type",
        "project-unknown",
        "project-source-unknown",
        "project-source-disconnected",
        "project-source-invalid",
        "project-source-token-refused",
        "project-source-unavailable",
        "durable-state-corrupt",
        "internal-error",
    ),
    (RUNS_PATH, "post"): (
        "invalid-revision-hash",
        "invalid-request",
        "unsupported-media-type",
        "workflow-revision-not-found",
        "invalid-agent-bindings",
        "uncast-agent-roles",
        "binding-constraint-refused",
        "agent-mode-mismatch",
        "agent-configuration-revision-not-found",
        "agent-executor-binding-unavailable",
        "run-identity-conflict",
        "workflow-format-not-executable",
        "run-input-refused",
        # A start whose order names a work item reads the connected project's
        # tracker, so this door answers that connection's own problems too.
        "project-source-not-connected",
        "project-source-unavailable",
        "project-source-payload-malformed",
        "durable-projection-unrepresentable",
        "temporarily-unavailable",
        "durable-state-corrupt",
        "internal-error",
    ),
    (RUNS_PATH, "get"): (
        "invalid-public-run-reference",
        "invalid-request",
        "durable-projection-unrepresentable",
        "temporarily-unavailable",
        "durable-state-corrupt",
        "internal-error",
    ),
    (API_PREFIX + "/runs/{public_ref}", "get"): (
        "invalid-public-run-reference",
        "run-not-found",
        "durable-projection-unrepresentable",
        "temporarily-unavailable",
        "durable-state-corrupt",
        "internal-error",
    ),
    (RUN_FORK_PATH, "post"): (
        "invalid-public-run-reference",
        "invalid-request",
        "unsupported-media-type",
        "run-not-found",
        "run-fork-origin-not-terminal",
        "run-fork-node-missing",
        "run-fork-loop-unsupported",
        "run-fork-prefix-not-reusable",
        "run-fork-command-conflict",
        "agent-executor-binding-unavailable",
        "agent-mode-mismatch",
        "durable-projection-unrepresentable",
        "temporarily-unavailable",
        "durable-state-corrupt",
        "internal-error",
    ),
    (API_PREFIX + "/runs/{public_ref}/nodes/{node_id}", "get"): (
        "invalid-public-run-reference",
        "run-not-found",
        "node-not-found",
        "durable-projection-unrepresentable",
        "temporarily-unavailable",
        "durable-state-corrupt",
        "internal-error",
    ),
    (CANCELLATION_PATH, "post"): (
        "invalid-public-run-reference",
        "invalid-agent-attempt-id",
        "invalid-request",
        "unsupported-media-type",
        "run-not-found",
        "agent-attempt-not-found",
        "agent-attempt-not-current",
        "agent-attempt-cancellation-stale",
        "agent-attempt-terminal",
        "cancellation-command-conflict",
        "replacement-not-allowed",
        "durable-projection-unrepresentable",
        "temporarily-unavailable",
        "durable-state-corrupt",
        "internal-error",
    ),
    (API_PREFIX + "/runs/{public_ref}/answers", "post"): (
        "invalid-public-run-reference",
        "invalid-revision-hash",
        "invalid-base64",
        "invalid-request",
        "unsupported-media-type",
        "run-not-found",
        "node-not-found",
        "answer-revision-conflict",
        "answer-state-conflict",
        "answer-execution-stale",
        "durable-projection-unrepresentable",
        "temporarily-unavailable",
        "durable-state-corrupt",
        "internal-error",
    ),
    (API_PREFIX + "/runs/{public_ref}/reconciliations", "post"): (
        "invalid-public-run-reference",
        "invalid-base64",
        "invalid-request",
        "unsupported-media-type",
        "run-not-found",
        "reconciliation-target-missing",
        "reconciliation-stale",
        "reconciliation-command-conflict",
        "reconciliation-determination-conflict",
        "reconciliation-rejected",
        "durable-projection-unrepresentable",
        "temporarily-unavailable",
        "durable-state-corrupt",
        "internal-error",
    ),
    (RUN_CANCELLATION_PATH, "post"): (
        "invalid-public-run-reference",
        "invalid-request",
        "unsupported-media-type",
        "run-not-found",
        "run-not-cancellable",
        "run-cancellation-command-conflict",
        "run-cancellation-overtaken-by-success",
        "durable-projection-unrepresentable",
        "temporarily-unavailable",
        "durable-state-corrupt",
        "internal-error",
    ),
    (EVENT_PATH, "get"): (
        "invalid-public-run-reference",
        "invalid-event-cursor",
        "event-cursor-run-mismatch",
        "event-cursor-ahead",
        "not-acceptable",
        "run-not-found",
        "durable-projection-unrepresentable",
        "temporarily-unavailable",
        "durable-state-corrupt",
        "internal-error",
    ),
    (ATTENTION_EVENT_PATH, "get"): (
        "invalid-event-cursor",
        "not-acceptable",
        "durable-projection-unrepresentable",
        "temporarily-unavailable",
        "durable-state-corrupt",
        "internal-error",
    ),
    (QUEUE_ADMISSIONS_PATH, "post"): (
        "invalid-request",
        "unsupported-media-type",
        "queue-admission-revision-conflict",
        "queue-admission-already-decided",
        "queue-admission-authority-refused",
        "queue-admission-proposal-required",
        "temporarily-unavailable",
        "durable-state-corrupt",
        "internal-error",
    ),
    (QUEUE_PROPOSALS_PATH, "put"): (
        "invalid-request",
        "unsupported-media-type",
        "queue-proposal-revision-conflict",
        "queue-proposal-already-decided",
        "queue-proposal-refused",
        "temporarily-unavailable",
        "durable-state-corrupt",
        "internal-error",
    ),
    (PROJECT_QUEUE_POLICY_PATH, "put"): (
        "invalid-public-project-reference",
        "invalid-request",
        "unsupported-media-type",
        "queue-policy-revision-conflict",
        "temporarily-unavailable",
        "durable-state-corrupt",
        "internal-error",
    ),
    (PROJECT_QUEUE_POLICY_PATH, "get"): (
        "invalid-public-project-reference",
        "queue-policy-not-set",
        "temporarily-unavailable",
        "durable-state-corrupt",
        "internal-error",
    ),
    (QUEUE_ITEMS_PATH, "get"): (
        "invalid-request",
        "temporarily-unavailable",
        "durable-state-corrupt",
        "internal-error",
    ),
    (PROJECT_SOURCE_IMPORT_PATH, "post"): (
        "project-source-not-connected",
        "project-source-unavailable",
        "project-source-payload-malformed",
        "temporarily-unavailable",
        "durable-state-corrupt",
        "internal-error",
    ),
}


def install_custom_openapi(app: FastAPI, limits: ApiLimits) -> None:
    def custom_openapi() -> dict[str, Any]:
        if app.openapi_schema is not None:
            return app.openapi_schema
        generated = get_openapi(
            title=app.title,
            version=app.version,
            openapi_version="3.1.0",
            routes=app.routes,
        )
        schema = deepcopy(generated)
        schema["openapi"] = "3.1.0"
        _remove_32_sse_fields(schema)
        _install_problem_components(schema)
        _install_problem_responses(schema)
        _install_workflow_document_grammar(schema)
        _install_publication_request_body(schema)
        _install_opaque_document_limits(schema, limits)
        _install_event_components(schema)
        _install_closed_start_union(schema)
        _install_sse_contract(schema)
        OpenAPI.model_validate(schema)
        app.openapi_schema = schema
        return schema

    app.openapi = custom_openapi
    custom_openapi()


def _install_workflow_document_grammar(schema: dict[str, Any]) -> None:
    """Publish the grammar a workflow document is read against, from its models.

    The document arrives as YAML bytes, so nothing the framework generates can
    describe its inside; without this, the one thing a consumer has to author
    himself is the one thing the API never says. It is derived rather than
    written: the same formats the parser dispatches on, each rendered from the
    model that decides it.
    """

    components = schema.setdefault("components", {}).setdefault("schemas", {})
    variants: list[dict[str, str]] = []
    for document_format in WORKFLOW_DOCUMENT_FORMATS.values():
        generated = document_format.model.model_json_schema(
            ref_template=_COMPONENT_REFERENCE
        )
        for name, definition in generated.pop(_JSON_SCHEMA_DEFS_KEY, {}).items():
            _install_component(components, name, definition)
        _install_component(components, document_format.model.__name__, generated)
        variants.append(
            {"$ref": _COMPONENT_REFERENCE.format(model=document_format.model.__name__)}
        )
    _install_component(
        components,
        WORKFLOW_DOCUMENT_COMPONENT,
        {"description": WORKFLOW_DOCUMENT_GRAMMAR_SCOPE, "oneOf": variants},
    )


def _install_component(
    components: dict[str, Any], name: str, definition: dict[str, Any]
) -> None:
    """Add one derived component, refusing to publish a second meaning of a name."""

    published = components.get(name)
    if published is not None and published != definition:
        raise RuntimeError(f"two different components claim the name {name!r}")
    components[name] = definition


def _install_publication_request_body(schema: dict[str, Any]) -> None:
    schema["paths"][WORKFLOW_REVISIONS_PATH]["post"]["requestBody"] = {
        "required": True,
        "content": {
            "application/yaml": {
                "schema": {
                    "$ref": _COMPONENT_REFERENCE.format(
                        model=WORKFLOW_DOCUMENT_COMPONENT
                    )
                }
            }
        },
    }
    schema["paths"][API_PREFIX + "/schema-revisions"]["post"]["requestBody"] = {
        "required": True,
        "content": {
            _APPLICATION_JSON: {"schema": {"type": "string", "format": "binary"}}
        },
    }
    schema["paths"][API_PREFIX + "/tool-grant-revisions"]["post"]["requestBody"] = {
        "required": True,
        "content": {
            _APPLICATION_JSON: {"schema": {"type": "string", "format": "binary"}}
        },
    }
    schema["paths"][API_PREFIX + "/adapter-operation-revisions"]["post"][
        "requestBody"
    ] = {
        "required": True,
        "content": {
            _APPLICATION_JSON: {"schema": {"type": "string", "format": "binary"}}
        },
    }
    schema["paths"][AGENT_DEFINITION_REVISIONS_PATH]["post"]["requestBody"] = {
        "required": True,
        "content": {
            "text/markdown": {"schema": {"type": "string", "format": "binary"}}
        },
    }


def _install_opaque_document_limits(schema: dict[str, Any], limits: ApiLimits) -> None:
    """Publish the bounds each opaque-document door enforces, from the limits it reads.

    The body is opaque bytes, so `maxLength` there counts bytes -- the same
    number the body middleware refuses above -- and every string that travels
    beside it is one field under the bound every other query string meets.
    """

    for path in (LIBRARY_RECOGNITIONS_PATH, LIBRARY_ADDITIONS_PATH):
        operation = schema["paths"][path]["post"]
        operation["requestBody"] = {
            "required": True,
            "content": {
                "application/octet-stream": {
                    "schema": {
                        "type": "string",
                        "format": "binary",
                        "maxLength": limits.maximum_request_body_bytes,
                    }
                }
            },
        }
        for parameter in operation["parameters"]:
            if "$ref" in parameter["schema"]:
                continue
            parameter["schema"] = _bounded_field_schema(
                parameter["schema"], limits.maximum_field_characters
            )


def _bounded_field_schema(generated: dict[str, Any], maximum: int) -> dict[str, Any]:
    """One query string's schema, bounded, keeping whether it may be omitted."""

    bounded = {"type": "string", "maxLength": maximum}
    if "anyOf" in generated:
        return {"anyOf": [bounded, {"type": "null"}], "title": generated["title"]}
    return {**bounded, "title": generated["title"]}


def _remove_32_sse_fields(value: object) -> None:
    if isinstance(value, dict):
        value.pop("itemSchema", None)
        value.pop("contentSchema", None)
        for nested in value.values():
            _remove_32_sse_fields(nested)
    elif isinstance(value, list):
        for nested in value:
            _remove_32_sse_fields(nested)


def _problem_component_name(code: str) -> str:
    return "Problem" + "".join(part.title() for part in code.split("-"))


def _install_problem_components(schema: dict[str, Any]) -> None:
    components = schema.setdefault("components", {}).setdefault("schemas", {})
    components.setdefault(
        "InvalidFieldResource",
        {
            "type": "object",
            "additionalProperties": False,
            "required": ["path", "reason"],
            "properties": {
                "path": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": MAXIMUM_INVALID_FIELD_PATH_CHARACTERS,
                },
                "reason": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": MAXIMUM_INVALID_FIELD_REASON_CHARACTERS,
                },
            },
        },
    )
    components.setdefault(
        "UncastRoleResource",
        {
            "type": "object",
            "additionalProperties": False,
            "required": ["role", "reason"],
            "properties": {
                "role": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": MAXIMUM_AGENT_FIELD_CHARACTERS,
                },
                "reason": {
                    "type": "string",
                    "enum": [reason.value for reason in ModelResolutionUncastReason],
                },
                "family_differs_from": {
                    "oneOf": [
                        {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": MAXIMUM_AGENT_FIELD_CHARACTERS,
                        },
                        {"type": "null"},
                    ]
                },
            },
        },
    )
    for code, definition in PROBLEM_DEFINITIONS.items():
        required = ["type", "title", "status", "detail"]
        if code == "uncast-agent-roles":
            required.append("uncast_roles")
        components[_problem_component_name(code)] = {
            "type": "object",
            "additionalProperties": False,
            "required": required,
            "properties": {
                "type": {"type": "string", "const": PROBLEM_TYPE_PREFIX + code},
                "title": {"type": "string", "const": definition.title},
                "status": {"type": "integer", "const": definition.status},
                "detail": {"type": "string"},
                **(
                    {
                        "invalid_fields": {
                            "type": "array",
                            "items": {
                                "$ref": "#/components/schemas/InvalidFieldResource"
                            },
                        }
                    }
                    if code in ("invalid-request", "run-input-refused")
                    else {}
                ),
                **(
                    {
                        "uncast_roles": {
                            "type": "array",
                            "minItems": 1,
                            "items": {
                                "$ref": "#/components/schemas/UncastRoleResource"
                            },
                        }
                    }
                    if code == "uncast-agent-roles"
                    else {}
                ),
            },
        }


def _install_problem_responses(schema: dict[str, Any]) -> None:
    for (path, method), codes in OPERATION_PROBLEMS.items():
        operation = schema["paths"][path][method]
        operation["responses"].pop("422", None)
        grouped: defaultdict[int, list[str]] = defaultdict(list)
        for code in codes:
            grouped[PROBLEM_DEFINITIONS[code].status].append(code)
        for status, status_codes in grouped.items():
            operation["responses"][str(status)] = {
                "description": "RFC 9457 problem",
                "content": {
                    "application/problem+json": {
                        "schema": {
                            "oneOf": [
                                {
                                    "$ref": _COMPONENT_SCHEMA_PREFIX
                                    + _problem_component_name(code)
                                }
                                for code in status_codes
                            ]
                        }
                    }
                },
            }


def _stream_failure_component() -> dict[str, Any]:
    """The failure frame, with its problem narrowed to the codes the stream emits.

    The generated model would carry the open problem shape, which promises a
    consumer bodies neither the stream nor the closed REST vocabulary can send.
    """

    generated = StreamFailureResource.model_json_schema(
        mode="serialization", ref_template=_COMPONENT_REFERENCE
    )
    generated.pop(_JSON_SCHEMA_DEFS_KEY, None)
    generated["properties"]["problem"] = {
        "oneOf": [
            {"$ref": _COMPONENT_SCHEMA_PREFIX + _problem_component_name(code)}
            for code in STREAM_FAILURE_CODES
        ]
    }
    return generated


def _run_projection_corrupt_component() -> dict[str, Any]:
    """The attention-feed frame that names one unprojectable run.

    The wire model carries the durable-state-corrupt problem. The published
    component uses the REST problem name so one schema serves both doors.
    """

    generated = RunProjectionCorruptResource.model_json_schema(
        mode="serialization", ref_template=_COMPONENT_REFERENCE
    )
    generated.pop(_JSON_SCHEMA_DEFS_KEY, None)
    generated["properties"]["problem"] = {
        "$ref": _COMPONENT_SCHEMA_PREFIX
        + _problem_component_name("durable-state-corrupt")
    }
    return generated


def _install_event_components(schema: dict[str, Any]) -> None:
    components = schema.setdefault("components", {}).setdefault("schemas", {})
    for model in EVENT_MODELS_V3:
        generated = model.model_json_schema(
            mode="serialization", ref_template=_COMPONENT_REFERENCE
        )
        definitions = generated.pop(_JSON_SCHEMA_DEFS_KEY, {})
        components.update(definitions)
        components[model.__name__] = generated
    components["RunEventResourceV3"] = {
        "oneOf": [
            {"$ref": f"#/components/schemas/{model.__name__}"}
            for model in EVENT_MODELS_V3
        ],
        "description": (
            "The AGENT_FAILED forms are closed by their required shape: an "
            "attempt failure names failure_code and an attempt; a pre-attempt "
            "refusal names its product reason and, as detail, the sentence "
            "the refusing boundary gave, null where it gave none."
        ),
    }
    components["VersionedRunEventResource"] = {
        "oneOf": [{"$ref": "#/components/schemas/RunEventResourceV3"}]
    }
    components[StreamFailureResource.__name__] = _stream_failure_component()
    components[RunProjectionCorruptResource.__name__] = (
        _run_projection_corrupt_component()
    )
    components["EventCursor"] = {
        "type": "string",
        "pattern": EVENT_CURSOR_PATTERN,
    }
    components["PublicProjectReference"] = {
        "type": "string",
        "pattern": PUBLIC_PROJECT_REFERENCE_PATTERN,
        "maxLength": MAXIMUM_PUBLIC_PROJECT_REFERENCE_CHARACTERS,
    }
    schema["components"]["schemas"]["ProjectResource"]["properties"][
        "public_project_reference"
    ] = {"$ref": "#/components/schemas/PublicProjectReference"}
    components["PublicSourceReference"] = {
        "type": "string",
        "pattern": PUBLIC_SOURCE_REFERENCE_PATTERN,
        "maxLength": MAXIMUM_PUBLIC_SOURCE_REFERENCE_CHARACTERS,
    }
    schema["components"]["schemas"]["ProjectSourceResource"]["properties"][
        "public_source_reference"
    ] = {"$ref": "#/components/schemas/PublicSourceReference"}


def _install_closed_start_union(schema: dict[str, Any]) -> None:
    """The start body is a closed set of shapes, not an open list of candidates.

    A caller starts a run in exactly one of the three published shapes, and the
    shape it sends is what selects the start it asked for. FastAPI generates a
    Python union as `anyOf`, which says a body may satisfy several at once; the
    door does not work that way, so the published document says `oneOf`.
    """

    body = schema["paths"][RUNS_PATH]["post"]["requestBody"]["content"][
        _APPLICATION_JSON
    ]["schema"]
    variants = body.pop("anyOf", None)
    if not isinstance(variants, list):
        raise TypeError("generated OpenAPI omitted the start union")
    body["oneOf"] = variants


def _durable_and_failure_frames() -> dict[str, Any]:
    return {
        "durable_event": {
            "id": {"$ref": _EVENT_CURSOR_COMPONENT_REF},
            "data": {"$ref": "#/components/schemas/VersionedRunEventResource"},
        },
        "terminal_failure": {
            "data": {"$ref": "#/components/schemas/StreamFailureResource"}
        },
    }


def _install_sse_contract(schema: dict[str, Any]) -> None:
    _install_sse_path(schema, EVENT_PATH, _durable_and_failure_frames())
    attention_frames = _durable_and_failure_frames()
    attention_frames["run_projection_corrupt"] = {
        "id": {"$ref": _EVENT_CURSOR_COMPONENT_REF},
        "data": {"$ref": "#/components/schemas/RunProjectionCorruptResource"},
    }
    _install_sse_path(schema, ATTENTION_EVENT_PATH, attention_frames)


def _install_sse_path(
    schema: dict[str, Any], path: str, frames: dict[str, Any]
) -> None:
    operation = schema["paths"][path]["get"]
    operation.setdefault("parameters", []).append(
        {
            "name": "Last-Event-ID",
            "in": "header",
            "required": False,
            "schema": {"$ref": _EVENT_CURSOR_COMPONENT_REF},
            "description": "Cursor of the last event fully processed by the client.",
        }
    )
    response = operation["responses"]["200"]
    response["content"] = {
        "text/event-stream": {
            "schema": {"type": "string"},
            "x-atelier2-sse-v1": frames,
        }
    }
