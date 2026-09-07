from __future__ import annotations

from dataclasses import dataclass, field
from typing import cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from atelier2.adapters.markdown_agent_definitions import (
    parse_agent_definition,
    render_agent_definition,
)
from atelier2.adapters.yaml_workflows import parse_executable_workflow_document
from atelier2.api.app import create_app
from atelier2.api.openapi import API_PREFIX
from atelier2.api.projection.events import run_event_resource
from atelier2.contracts.agent_attempts import AgentAttemptId
from atelier2.contracts.agents import (
    AgentConfigurationRevision,
    AgentConfigurationRevisionFormatVersion,
    AgentConfigurationRevisionHash,
    AgentConfigurationRevisionListItem,
    AgentExecutionCapability,
    AgentExecutionRequestHash,
    AgentExecutorRevision,
    AuthMode,
    AuthProfileRevision,
    AuthProfileRevisionHash,
    ProviderId,
    ProviderProbeFailure,
)
from atelier2.contracts.executions import (
    NodeExecutionId,
    RunEvent,
    RunEventAgentAttemptBinding,
    RunEventKind,
)
from atelier2.contracts.provider_probe_receipts import ProviderProbeProblemCode
from atelier2.contracts.run_events import (
    PersistedRunEvent,
)
from atelier2.contracts.runs import RunId, WorkflowRevision
from atelier2.contracts.when import RecordedAt
from atelier2.contracts.workflow_formats import WorkflowFormatVersion
from atelier2.ports.agent_configurations import (
    AgentConfigurationRevisionCollision,
    AgentConfigurationRevisionCreated,
    AgentConfigurationRevisionPage,
    AgentExecutorBindingUnavailable,
    AuthProfileRevisionCollision,
    AuthProfileRevisionConflict,
    AuthProfileRevisionCreated,
    AuthProfileRevisionMissing,
    AuthProfileRevisionPage,
    CatalogReadUnavailable,
)
from atelier2.ports.durable_runs import (
    DurableStateCorrupt,
    DurableWriteUnavailable,
)
from tests.api.test_agent_attempts import SERVED_RAIL
from tests.scenarios.api import (
    api_limits,
    api_ports,
    event_poll_backoff,
)
from tests.scenarios.workflows import V3_DOCUMENT

AUTH = AuthProfileRevision("max", 7, ProviderId("anthropic"), AuthMode.SUBSCRIPTION)
CONFIGURATION = AgentConfigurationRevision(
    "claude-opus-4-1",
    AUTH.revision_hash,
    AgentExecutorRevision("claude-cli/v1"),
    AgentExecutionCapability.HEADLESS,
    AgentConfigurationRevisionFormatVersion.V2,
)
INTERACTIVE_CONFIGURATION_HASH = (
    "d881bf2700c0d88b37704959cfb3c44a6bb575e9c1ec93c09650f95aa8ac9279"
)


@dataclass
class RecordingCatalog:
    """A catalog that publishes exactly the revision the route hands it.

    A configured `configuration_result` answers instead, which is how a refusal
    is asked for; without one the response echoes the route's own construction,
    so what the caller reads back is the configuration that would be stored.
    """

    auth_result: object
    configuration_result: object | None = None
    list_result: object = AgentConfigurationRevisionPage((), None)
    auth_list_result: object = AuthProfileRevisionPage((), None)
    auth_calls: int = 0
    configuration_calls: int = 0
    list_calls: list[tuple[object, int]] = field(default_factory=list)
    auth_list_calls: list[tuple[object, int]] = field(default_factory=list)

    def publish_auth_profile_revision(self, revision: AuthProfileRevision) -> object:
        self.auth_calls += 1
        assert revision == AUTH
        return self.auth_result

    def publish_agent_configuration_revision(
        self, revision: AgentConfigurationRevision
    ) -> object:
        self.configuration_calls += 1
        if self.configuration_result is None:
            return AgentConfigurationRevisionCreated(revision, AUTH)
        return self.configuration_result

    def agent_configuration_revision(self, _revision_hash: object) -> None:
        return None

    def list_agent_configuration_revisions(self, after: object, limit: int) -> object:
        self.list_calls.append((after, limit))
        return self.list_result

    def list_auth_profile_revisions(self, after: object, limit: int) -> object:
        self.auth_list_calls.append((after, limit))
        return self.auth_list_result


def _client(catalog: RecordingCatalog) -> TestClient:
    return TestClient(
        create_app(
            source_commit="commit",
            source_tree="tree",
            ports=api_ports(
                workflow_document_parser=parse_executable_workflow_document,
                agent_definition_parser=parse_agent_definition,
                agent_definition_renderer=render_agent_definition,
                agent_configuration_catalog=catalog,
            ),
            limits=api_limits(),
            event_poll_backoff=event_poll_backoff(),
        )
    )


def _publish_auth(client: TestClient):
    return client.post(
        API_PREFIX + "/auth-profile-revisions",
        json={
            "profile_id": "max",
            "revision_number": 7,
            "provider_id": "anthropic",
            "auth_mode": "subscription",
        },
    )


def _publish_configuration(client: TestClient, **capability: object):
    return client.post(
        API_PREFIX + "/agent-configuration-revisions",
        json={
            "model": "claude-opus-4-1",
            "auth_profile_revision_hash": AUTH.revision_hash.value,
            "executor_revision": "claude-cli/v1",
            **capability,
        },
    )


def test_publish_resources_have_the_exact_secret_free_wire_shape() -> None:
    catalog = RecordingCatalog(AuthProfileRevisionCreated(AUTH))
    client = _client(catalog)

    auth = _publish_auth(client)
    configuration = _publish_configuration(client)

    assert auth.status_code == 201
    assert auth.json() == {
        "profile_id": "max",
        "revision_number": 7,
        "provider_id": "anthropic",
        "auth_mode": "subscription",
        "auth_profile_revision_hash": AUTH.revision_hash.value,
    }
    assert configuration.status_code == 201
    assert configuration.json() == {
        "model": "claude-opus-4-1",
        "auth_profile_revision_hash": AUTH.revision_hash.value,
        "executor_revision": "claude-cli/v1",
        "provider_id": "anthropic",
        "auth_mode": "subscription",
        "requested_capability": "headless",
        "agent_configuration_revision_hash": CONFIGURATION.revision_hash.value,
    }
    assert all(
        forbidden not in (auth.text + configuration.text).lower()
        for forbidden in ("secret", "credential", "handle", "api_key_value")
    )


@pytest.mark.parametrize(
    ("published", "echoed", "configuration_hash"),
    [
        ({}, "headless", CONFIGURATION.revision_hash.value),
        (
            {"requested_capability": "headless"},
            "headless",
            CONFIGURATION.revision_hash.value,
        ),
        (
            {"requested_capability": "interactive"},
            "interactive",
            INTERACTIVE_CONFIGURATION_HASH,
        ),
    ],
)
def test_the_requested_capability_decides_the_published_configuration(
    published: dict[str, object], echoed: str, configuration_hash: str
) -> None:
    """What a caller asks for is what is published, and what identifies it.

    Omitting the field and asking for `headless` are the same publication as
    the one this API made before the field existed, down to the hash; asking
    for `interactive` publishes a different configuration, which is why the
    interactive identity is authored here rather than derived.
    """

    client = _client(RecordingCatalog(AuthProfileRevisionCreated(AUTH)))

    response = _publish_configuration(client, **published)

    assert response.status_code == 201
    assert response.json()["requested_capability"] == echoed
    assert response.json()["agent_configuration_revision_hash"] == configuration_hash


@pytest.mark.parametrize(
    "requested_capability",
    [None, "Headless", "INTERACTIVE", "supervised", "", 1],
)
def test_an_uncontracted_capability_is_refused_before_any_catalog_effect(
    requested_capability: object,
) -> None:
    catalog = RecordingCatalog(AuthProfileRevisionCreated(AUTH))

    response = _publish_configuration(
        _client(catalog), requested_capability=requested_capability
    )

    assert response.status_code == 422
    assert response.json()["type"].endswith(":invalid-request")
    assert catalog.configuration_calls == 0


@pytest.mark.parametrize(
    ("result", "status", "code"),
    [
        (AuthProfileRevisionConflict(), 409, "auth-profile-revision-conflict"),
        (AuthProfileRevisionCollision(), 409, "auth-profile-revision-collision"),
        (DurableWriteUnavailable(), 503, "temporarily-unavailable"),
        (DurableStateCorrupt(), 500, "durable-state-corrupt"),
    ],
)
def test_auth_publish_maps_every_refusal(
    result: object, status: int, code: str
) -> None:
    response = _publish_auth(_client(RecordingCatalog(result, object())))

    assert response.status_code == status
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["type"] == f"urn:atelier2:problem:v1:{code}"


@pytest.mark.parametrize(
    ("result", "status", "code"),
    [
        (AuthProfileRevisionMissing(), 404, "auth-profile-revision-not-found"),
        (
            AgentExecutorBindingUnavailable(),
            409,
            "agent-executor-binding-unavailable",
        ),
        (
            AgentConfigurationRevisionCollision(),
            409,
            "agent-configuration-revision-collision",
        ),
        (DurableWriteUnavailable(), 503, "temporarily-unavailable"),
        (DurableStateCorrupt(), 500, "durable-state-corrupt"),
    ],
)
def test_configuration_publish_maps_every_refusal(
    result: object, status: int, code: str
) -> None:
    response = _publish_configuration(_client(RecordingCatalog(object(), result)))

    assert response.status_code == status
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["type"] == f"urn:atelier2:problem:v1:{code}"


def test_invalid_provider_is_rejected_before_catalog_effect() -> None:
    catalog = RecordingCatalog(object(), object())
    response = _client(catalog).post(
        API_PREFIX + "/auth-profile-revisions",
        json={
            "profile_id": "max",
            "revision_number": 7,
            "provider_id": "Anthropic",
            "auth_mode": "subscription",
        },
    )

    assert response.status_code == 422
    assert response.json()["type"].endswith(":invalid-request")
    assert catalog.auth_calls == 0


@pytest.mark.parametrize(
    "extra",
    [
        {"workflow_format_version": 2},
        {"agent_bindings": []},
    ],
)
def test_malformed_versioned_start_is_the_exact_agent_bindings_problem(
    extra: dict[str, object],
) -> None:
    client = _client(RecordingCatalog(object(), object()))
    body: dict[str, object] = {
        "run_id": "v2/malformed",
        "workflow_revision_hash": "0" * 64,
        **extra,
    }

    response = client.post(API_PREFIX + "/runs", json=body)

    assert response.status_code == 422
    assert response.json()["type"].endswith(":invalid-agent-bindings")


def test_openapi_names_both_publish_operations_and_exact_problem_sets() -> None:
    schema = cast(FastAPI, _client(RecordingCatalog(object(), object())).app).openapi()

    for path in (
        API_PREFIX + "/auth-profile-revisions",
        API_PREFIX + "/agent-configuration-revisions",
    ):
        operation = schema["paths"][path]["post"]
        assert set(operation["responses"]) >= {"200", "201", "409", "422", "500", "503"}
        assert set(operation["requestBody"]["content"]) == {"application/json"}
    configuration_responses = schema["paths"][
        API_PREFIX + "/agent-configuration-revisions"
    ]["post"]["responses"]
    assert "404" in configuration_responses

    start = schema["paths"][API_PREFIX + "/runs"]["post"]
    assert [
        item["$ref"]
        for item in start["requestBody"]["content"]["application/json"]["schema"][
            "oneOf"
        ]
    ] == [
        "#/components/schemas/StartRunRequestResource",
        "#/components/schemas/StartRunRequestResourceV2",
        "#/components/schemas/StartRunRequestResourceV3",
    ]
    assert start["responses"]["201"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/RunResourceV3"
    }
    assert schema["paths"][API_PREFIX + "/runs"]["get"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"] == {"$ref": "#/components/schemas/VersionedRunPageResource"}
    run_row_items = schema["components"]["schemas"]["VersionedRunPageResource"][
        "properties"
    ]["items"]["items"]
    assert run_row_items["discriminator"]["propertyName"] == "kind"
    assert {item["$ref"] for item in run_row_items["oneOf"]} == {
        "#/components/schemas/RunListRowResource",
        "#/components/schemas/DefectiveRunRowResource",
    }
    assert schema["components"]["schemas"]["RunListRowResource"]["properties"][
        "run"
    ] == {"$ref": "#/components/schemas/RunResourceV3"}
    graph = schema["components"]["schemas"]["WorkflowRevisionDetailResource"][
        "properties"
    ]["graph"]
    assert graph == {"$ref": "#/components/schemas/WorkflowGraphResourceV3"}


def test_auth_list_answers_with_the_published_item_form_and_no_secrets() -> None:
    catalog = RecordingCatalog(
        object(),
        auth_list_result=AuthProfileRevisionPage((AUTH,), None),
    )

    response = _client(catalog).get(API_PREFIX + "/auth-profile-revisions")

    assert response.status_code == 200
    assert response.json() == {
        "items": [
            {
                "profile_id": "max",
                "revision_number": 7,
                "provider_id": "anthropic",
                "auth_mode": "subscription",
                "auth_profile_revision_hash": AUTH.revision_hash.value,
            }
        ],
        "next_after_revision_hash": None,
    }
    assert all(
        forbidden not in response.text.lower()
        for forbidden in ("secret", "credential", "handle", "api_key_value")
    )
    assert catalog.auth_list_calls == [(None, 50)]


def test_auth_list_pages_with_the_workflow_revision_cursor() -> None:
    catalog = RecordingCatalog(
        object(),
        auth_list_result=AuthProfileRevisionPage((AUTH,), AUTH.revision_hash),
    )

    response = _client(catalog).get(
        API_PREFIX + "/auth-profile-revisions",
        params={"after_revision_hash": "a" * 64, "limit": "1"},
    )

    assert response.status_code == 200
    assert response.json()["next_after_revision_hash"] == AUTH.revision_hash.value
    after, limit = catalog.auth_list_calls[0]
    assert isinstance(after, AuthProfileRevisionHash)
    assert after.value == "a" * 64
    assert limit == 1


def test_auth_list_empty_is_an_empty_page() -> None:
    response = _client(RecordingCatalog(object())).get(
        API_PREFIX + "/auth-profile-revisions"
    )

    assert response.status_code == 200
    assert response.json() == {"items": [], "next_after_revision_hash": None}


def test_auth_list_refuses_a_malformed_cursor_before_the_catalog() -> None:
    catalog = RecordingCatalog(object())

    response = _client(catalog).get(
        API_PREFIX + "/auth-profile-revisions",
        params={"after_revision_hash": "not-a-hash"},
    )

    assert response.status_code == 400
    assert response.json()["type"].endswith(":invalid-revision-hash")
    assert catalog.auth_list_calls == []


@pytest.mark.parametrize(
    ("result", "status", "code"),
    [
        (CatalogReadUnavailable("store asleep"), 503, "temporarily-unavailable"),
        (DurableStateCorrupt(), 500, "durable-state-corrupt"),
    ],
)
def test_auth_list_maps_every_read_refusal(
    result: object, status: int, code: str
) -> None:
    response = _client(RecordingCatalog(object(), auth_list_result=result)).get(
        API_PREFIX + "/auth-profile-revisions"
    )

    assert response.status_code == status
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["type"] == f"urn:atelier2:problem:v1:{code}"


def test_list_answers_with_the_published_item_form_and_no_secrets() -> None:
    catalog = RecordingCatalog(
        object(),
        list_result=AgentConfigurationRevisionPage(
            (
                AgentConfigurationRevisionListItem(
                    CONFIGURATION, AUTH, True, True, True
                ),
            ),
            None,
        ),
    )

    response = _client(catalog).get(API_PREFIX + "/agent-configuration-revisions")

    assert response.status_code == 200
    assert response.json() == {
        "items": [
            {
                "model": "claude-opus-4-1",
                "auth_profile_revision_hash": AUTH.revision_hash.value,
                "executor_revision": "claude-cli/v1",
                "provider_id": "anthropic",
                "auth_mode": "subscription",
                "requested_capability": "headless",
                "agent_configuration_revision_hash": CONFIGURATION.revision_hash.value,
                "startable": True,
                "structurally_startable": True,
                "not_startable_reason": None,
                "provider_probe_problem_code": None,
                "provider_probe_observed_at": None,
            }
        ],
        "next_after_revision_hash": None,
    }
    assert all(
        forbidden not in response.text.lower()
        for forbidden in ("secret", "credential", "handle", "api_key_value")
    )
    assert catalog.list_calls == [(None, 50)]


def test_list_pages_with_the_workflow_revision_cursor() -> None:
    catalog = RecordingCatalog(
        object(),
        list_result=AgentConfigurationRevisionPage(
            (
                AgentConfigurationRevisionListItem(
                    CONFIGURATION, AUTH, True, True, True
                ),
            ),
            CONFIGURATION.revision_hash,
        ),
    )

    response = _client(catalog).get(
        API_PREFIX + "/agent-configuration-revisions",
        params={"after_revision_hash": "a" * 64, "limit": "1"},
    )

    assert response.status_code == 200
    assert (
        response.json()["next_after_revision_hash"] == CONFIGURATION.revision_hash.value
    )
    after, limit = catalog.list_calls[0]
    assert isinstance(after, AgentConfigurationRevisionHash)
    assert after.value == "a" * 64
    assert limit == 1


def test_list_marks_a_declared_but_unstartable_configuration_without_diagnostics() -> (
    None
):
    catalog = RecordingCatalog(
        object(),
        list_result=AgentConfigurationRevisionPage(
            (
                AgentConfigurationRevisionListItem(
                    CONFIGURATION, AUTH, False, False, False
                ),
            ),
            None,
        ),
    )

    response = _client(catalog).get(API_PREFIX + "/agent-configuration-revisions")

    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["startable"] is False
    assert item["structurally_startable"] is False
    assert item["not_startable_reason"] == "agent-executor-binding-unavailable"
    assert "diagnostic" not in response.text.lower()


def test_list_names_a_missing_receipt_apart_from_an_unavailable_executor() -> None:
    """Structurally ready but not evidentially proven names its own reason.

    The two questions the registry now answers separately must not collapse
    back into one on the wire: an executor with no problem at all beyond a
    missing live receipt is not "binding unavailable" -- that would point an
    operator at the wrong half of the deployment.
    """

    catalog = RecordingCatalog(
        object(),
        list_result=AgentConfigurationRevisionPage(
            (
                AgentConfigurationRevisionListItem(
                    CONFIGURATION, AUTH, True, True, False
                ),
            ),
            None,
        ),
    )

    response = _client(catalog).get(API_PREFIX + "/agent-configuration-revisions")

    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["startable"] is False
    assert item["structurally_startable"] is True
    assert item["not_startable_reason"] == "provider-probe-receipt-missing"
    assert item["provider_probe_problem_code"] is None
    assert item["provider_probe_observed_at"] is None


def test_list_names_a_failed_probe_apart_from_a_merely_missing_receipt() -> None:
    """A receipt that recorded a failure names itself, with its own evidence.

    #1103: an operator reading "provider-probe-receipt-missing" for a
    configuration whose last probe actually ran and failed is misled into
    waiting for a probe that already happened -- the wire must carry the
    failure's own problem code and instant instead of collapsing it into the
    same reason a never-probed configuration gets.
    """

    catalog = RecordingCatalog(
        object(),
        list_result=AgentConfigurationRevisionPage(
            (
                AgentConfigurationRevisionListItem(
                    CONFIGURATION,
                    AUTH,
                    True,
                    True,
                    False,
                    ProviderProbeFailure(
                        ProviderProbeProblemCode("provider-overloaded"),
                        RecordedAt("2026-09-03T16:17:00Z"),
                    ),
                ),
            ),
            None,
        ),
    )

    response = _client(catalog).get(API_PREFIX + "/agent-configuration-revisions")

    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["startable"] is False
    assert item["structurally_startable"] is True
    assert item["not_startable_reason"] == "provider-probe-failed"
    assert item["provider_probe_problem_code"] == "provider-overloaded"
    assert item["provider_probe_observed_at"] == "2026-09-03T16:17:00Z"


def test_list_names_a_superseded_model_apart_from_a_missing_receipt() -> None:
    """A registry pointer that moved on names its own reason, before receipts.

    A structurally startable configuration the model registry no longer
    points to is not "merely unproven": a start's cast would refuse it
    (`uncast-agent-roles`) before any receipt is even asked for, so the
    listing must not offer it as though a fresh probe would fix it.
    """

    catalog = RecordingCatalog(
        object(),
        list_result=AgentConfigurationRevisionPage(
            (
                AgentConfigurationRevisionListItem(
                    CONFIGURATION, AUTH, True, False, True
                ),
            ),
            None,
        ),
    )

    response = _client(catalog).get(API_PREFIX + "/agent-configuration-revisions")

    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["startable"] is False
    assert item["structurally_startable"] is True
    assert item["not_startable_reason"] == "model-not-registered"


def test_list_names_a_superseded_model_over_its_own_failed_receipt() -> None:
    """#1128: the reason the wire names must be the evidence the wire carries.

    A superseded model outranks its own failed receipt in the precedence
    (`test_list_names_a_superseded_model_apart_from_a_missing_receipt`), but
    the receipt can itself have failed rather than merely being missing --
    live: a config demoted by a newer revision while its last probe was
    still recorded as a failure. The response must answer 200 with
    `model-not-registered` and no probe evidence, never surface the failed
    receipt's evidence for a reason that does not name it.
    """

    catalog = RecordingCatalog(
        object(),
        list_result=AgentConfigurationRevisionPage(
            (
                AgentConfigurationRevisionListItem(
                    CONFIGURATION,
                    AUTH,
                    True,
                    False,
                    False,
                    ProviderProbeFailure(
                        ProviderProbeProblemCode("provider-overloaded"),
                        RecordedAt("2026-09-03T11:22:00Z"),
                    ),
                ),
            ),
            None,
        ),
    )

    response = _client(catalog).get(API_PREFIX + "/agent-configuration-revisions")

    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["startable"] is False
    assert item["structurally_startable"] is True
    assert item["not_startable_reason"] == "model-not-registered"
    assert item["provider_probe_problem_code"] is None
    assert item["provider_probe_observed_at"] is None


def test_list_empty_is_an_empty_page() -> None:
    response = _client(RecordingCatalog(object())).get(
        API_PREFIX + "/agent-configuration-revisions"
    )

    assert response.status_code == 200
    assert response.json() == {"items": [], "next_after_revision_hash": None}


def test_list_refuses_a_malformed_cursor_before_the_catalog() -> None:
    catalog = RecordingCatalog(object())

    response = _client(catalog).get(
        API_PREFIX + "/agent-configuration-revisions",
        params={"after_revision_hash": "not-a-hash"},
    )

    assert response.status_code == 400
    assert response.json()["type"].endswith(":invalid-revision-hash")
    assert catalog.list_calls == []


@pytest.mark.parametrize(
    ("result", "status", "code"),
    [
        (CatalogReadUnavailable("store asleep"), 503, "temporarily-unavailable"),
        (DurableStateCorrupt(), 500, "durable-state-corrupt"),
    ],
)
def test_list_maps_every_read_refusal(result: object, status: int, code: str) -> None:
    response = _client(RecordingCatalog(object(), list_result=result)).get(
        API_PREFIX + "/agent-configuration-revisions"
    )

    assert response.status_code == status
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["type"] == f"urn:atelier2:problem:v1:{code}"


def test_agent_event_roundtrips_arbitrary_bytes_as_canonical_base64() -> None:
    workflow = WorkflowRevision(V3_DOCUMENT)
    run_id = RunId("v3/non-utf8")
    event = RunEvent(
        run_id,
        workflow.revision_hash,
        1,
        "build",
        NodeExecutionId.for_node(run_id, workflow.revision_hash, "build"),
        RunEventKind.AGENT_COMPLETED,
        b"\x00\xffoutput",
        attempt_binding=RunEventAgentAttemptBinding(
            AgentAttemptId.for_execution(
                NodeExecutionId.for_node(run_id, workflow.revision_hash, "build"),
                AgentExecutionRequestHash("1" * 64),
                1,
            ),
            1,
        ),
    )

    resource = run_event_resource(
        PersistedRunEvent(event, None, WorkflowFormatVersion.V3), SERVED_RAIL
    )
    api_limits().require_event_projection(
        PersistedRunEvent(event, None, WorkflowFormatVersion.V3)
    )
    assert event.attempt_binding is not None

    assert resource.model_dump(mode="json") == {
        "workflow_format_version": 3,
        "node_rail": [
            {"node_id": "build", "state": "working", "attempt": None},
            {"node_id": "done", "state": "queued", "attempt": None},
        ],
        "cursor": "event1.djMvbm9uLXV0Zjg.1",
        "sequence": 1,
        "public_run_reference": "run1.djMvbm9uLXV0Zjg",
        "workflow_revision_hash": workflow.revision_hash.value,
        "node_id": "build",
        "node_execution_id": event.node_execution_id.value,
        "event_hash": event.event_hash.value,
        "event": "AGENT_COMPLETED",
        "output_base64": "AP9vdXRwdXQ=",
        "output_hash": event.payload_hash.value,
        "attempt_id": event.attempt_binding.attempt_id.value,
        "attempt_ordinal": 1,
    }


def test_openapi_sse_data_is_the_untagged_served_event_union() -> None:
    schema = cast(FastAPI, _client(RecordingCatalog(object(), object())).app).openapi()

    data = schema["paths"][API_PREFIX + "/runs/{public_ref}/events"]["get"][
        "responses"
    ]["200"]["content"]["text/event-stream"]["x-atelier2-sse-v1"]["durable_event"][
        "data"
    ]
    assert data == {"$ref": "#/components/schemas/VersionedRunEventResource"}
    union = schema["components"]["schemas"]["VersionedRunEventResource"]
    assert union == {"oneOf": [{"$ref": "#/components/schemas/RunEventResourceV3"}]}
    common = {
        "workflow_format_version",
        "cursor",
        "sequence",
        "public_run_reference",
        "workflow_revision_hash",
        "node_id",
        "node_execution_id",
        "event_hash",
        "event",
        "node_rail",
    }
    payloads: dict[str, set[str]] = {
        "AGENT_COMPLETED": {
            "output_base64",
            "output_hash",
            "attempt_id",
            "attempt_ordinal",
        },
        "AGENT_FAILED": {"failure_code", "attempt_id", "attempt_ordinal"},
        "AGENT_CANCEL_REQUESTED": {
            "attempt_id",
            "attempt_ordinal",
            "command_id",
            "replacement",
        },
        "AGENT_CANCELLED": {
            "attempt_id",
            "attempt_ordinal",
            "command_id",
            "replacement",
            "disposition",
            "replacement_attempt_id",
        },
        "AGENT_INTERRUPTED": {
            "attempt_id",
            "attempt_ordinal",
            "command_id",
            "replacement",
            "disposition",
            "replacement_attempt_id",
        },
        "ACTION_RECONCILIATION_REQUIRED": {"request_base64", "request_hash"},
        "ACTION_RECONCILIATION_RESOLVED": {"receipt"},
        "ACTION_COMPLETED": {"receipt"},
        "WAITING_INPUT": set(),
        "WAIT_ANSWERED": {"actor", "answer_base64", "answer_hash"},
        "WAIT_CANCELLED": {"command_id"},
    }
    v3 = schema["components"]["schemas"]["RunEventResourceV3"]
    assert len(v3["oneOf"]) == 12
    assert "discriminator" not in v3
    assert v3["description"] == (
        "The AGENT_FAILED forms are closed by their required shape: an "
        "attempt failure names failure_code and an attempt; a pre-claim "
        "executor refusal names only its product reason."
    )
    v3_components = {reference["$ref"].rsplit("/", 1)[-1] for reference in v3["oneOf"]}
    assert v3_components == {
        "AgentCompletedEventResourceV3",
        "AgentFailedEventResourceV3",
        "AgentExecutorBindingUnavailableEventResourceV3",
        "AgentCancelRequestedEventResourceV3",
        "AgentCancelledEventResourceV3",
        "AgentInterruptedEventResourceV3",
        "ActionReconciliationRequiredEventResourceV3",
        "ActionReconciliationResolvedEventResourceV3",
        "ActionCompletedEventResourceV3",
        "WaitingInputEventResourceV3",
        "WaitAnsweredEventResourceV3",
        "WaitCancelledEventResourceV3",
    }
    for event, component_name in {
        "AGENT_COMPLETED": "AgentCompletedEventResourceV3",
        "AGENT_FAILED": "AgentFailedEventResourceV3",
        "AGENT_CANCEL_REQUESTED": "AgentCancelRequestedEventResourceV3",
        "AGENT_CANCELLED": "AgentCancelledEventResourceV3",
        "AGENT_INTERRUPTED": "AgentInterruptedEventResourceV3",
        "ACTION_RECONCILIATION_REQUIRED": "ActionReconciliationRequiredEventResourceV3",
        "ACTION_RECONCILIATION_RESOLVED": "ActionReconciliationResolvedEventResourceV3",
        "ACTION_COMPLETED": "ActionCompletedEventResourceV3",
        "WAITING_INPUT": "WaitingInputEventResourceV3",
        "WAIT_ANSWERED": "WaitAnsweredEventResourceV3",
        "WAIT_CANCELLED": "WaitCancelledEventResourceV3",
    }.items():
        component = schema["components"]["schemas"][component_name]
        extra = {"reason"} if event == "AGENT_FAILED" else set()
        expected_fields = common | payloads[event] | extra
        assert set(component["properties"]) == expected_fields
        assert set(component["required"]) == expected_fields
        assert component["additionalProperties"] is False
        assert component["properties"]["workflow_format_version"]["const"] == 3
    unavailable_v3 = schema["components"]["schemas"][
        "AgentExecutorBindingUnavailableEventResourceV3"
    ]
    assert set(unavailable_v3["properties"]) == common | {"reason", "detail"}
    assert set(unavailable_v3["required"]) == common | {"reason", "detail"}
    assert unavailable_v3["additionalProperties"] is False
    assert unavailable_v3["properties"]["workflow_format_version"]["const"] == 3


def test_openapi_has_no_private_credential_channel() -> None:
    schema = cast(FastAPI, _client(RecordingCatalog(object(), object())).app).openapi()
    forbidden_properties = {
        "secret",
        "secret_value",
        "credential",
        "credentials",
        "credential_handle",
        "credential_ref",
        "credential_path",
        "api_key_value",
        "key_name",
        "lookup_key",
    }

    property_names = {
        property_name
        for component in schema["components"]["schemas"].values()
        for property_name in component.get("properties", {})
    }

    assert property_names.isdisjoint(forbidden_properties)
