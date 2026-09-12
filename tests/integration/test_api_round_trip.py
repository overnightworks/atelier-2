"""Every request this consumer writes is assembled out of the answers before it.

**What is being proved.** Finding 2 of the #85 measurement said the API answers
with values a consumer cannot hand back unchanged: the same thing was called
`revision_hash` on one surface and `workflow_revision_hash` on another, the
format version split the same way, and the address `POST /artifacts` answered
had to be renamed to travel in an order. A consumer therefore had to carry a
translation table that lives nowhere on the wire.

**How this file proves it, rather than asserting it.** Nothing here writes a
field name next to a value read from a different field name. Every value that
crosses from an answer into the next request goes through `carried`, which can
only produce a field under the name the previous answer already used. A drift
does not make an assertion fail somewhere downstream -- it makes the request
unbuildable, at the line that tries to build it. Reverting any one of the
renames this change made kills this test where the drift is.

The four round-trips are the ones an operator's machine really walks: a name to
a start, a run reading to its answer, a catalog reading to a start, and a
published artifact to the order that names it. Discovery from a bare base URL is
the first head's proof (#322 Kopf 1) and is not repeated here; this consumer
starts where that one left off, at the versioned prefix.
"""

from __future__ import annotations

import time
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from httpx import Response

from atelier2.adapters.dbos.host_configuration import publish_project_root_revision
from atelier2.adapters.dbos.runtime import DbosRuntime
from atelier2.adapters.dbos.schema import (
    host_model_registry_revisions,
    host_project_model_defaults_revisions,
)
from atelier2.api.openapi import (
    API_PREFIX,
    CATALOG_LINEAGES_PATH,
    MODEL_REGISTRY_PATH,
)
from atelier2.api.references import (
    encode_canonical_base64,
    encode_public_project_reference,
)
from atelier2.contracts.agents import AgentConfigurationRevision, AuthProfileRevision
from atelier2.contracts.host_configuration import (
    ProjectId,
    ProjectRootRevision,
    ProviderModelCheck,
)
from atelier2.contracts.revisions_v3 import RevisionKind
from atelier2.contracts.run_projections import NodeState
from atelier2.contracts.runs import RunState
from atelier2.ports.host_configuration import (
    ProviderModelDiscoverer,
    ProviderModelDiscoveryResult,
    ProviderModelDiscoveryUnsupported,
    ProviderModelValidationResult,
    ProviderModelValidator,
)
from tests.scenarios.agents import (
    RecordingAgentExecutorFactoryV2,
    agent_scratch_root,
)
from tests.scenarios.api import durable_api_client
from tests.scenarios.durable_state import (
    canonical_loopback_effects,
    canonical_runtime_settings,
)

WORKFLOW_PATH = API_PREFIX + "/workflow-revisions"
LINEAGE_PATH = CATALOG_LINEAGES_PATH
BY_NAME_PATH = API_PREFIX + f"/catalog-revisions/by-name/{RevisionKind.WORKFLOW.value}"
SCHEMA_PATH = API_PREFIX + "/schema-revisions"
ARTIFACT_PATH = API_PREFIX + "/artifacts"
AUTH_PROFILE_PATH = API_PREFIX + "/auth-profile-revisions"
AGENT_CONFIGURATION_PATH = API_PREFIX + "/agent-configuration-revisions"
RUN_PATH = API_PREFIX + "/runs"
PROJECT_MODEL_DEFAULTS_PATH = (
    API_PREFIX + "/projects/{public_project_reference}/model-defaults"
)

ANY_JSON = b"true"
"""The schema an order and an agent output pin here: it admits every JSON value.

The subject of this file is which words a value travels under, not which values
a schema admits, so the narrowest useful schema is the one that never refuses.
The wait node below pins a schema that does refuse, because the answer this
consumer writes has to be a real answer to a real question."""

ONLY_A_STRING = b'{"type": "string"}'

PROVIDER_OUTPUT = b'"the exact provider bytes"'
APPROVAL = b'"approved"'
ORDER_MATERIAL = b'{"portions": 7}'
RUN_ID = "322/round-trip"
READ_DEADLINE_SECONDS = 8
READ_INTERVAL_SECONDS = 0.025


def workflow_document(order_schema: str, approval_schema: str) -> bytes:
    """One line that needs an order, an agent, a person, and a second agent.

    The two schema revisions are the ones the publication just answered with, so
    even the document a consumer authors is written out of an answer.
    """

    return f"""format_version: 3
name: cook-and-approve
graph_inputs:
  - name: order
    schema:
      ref: order-schema
      revision: {order_schema}
nodes:
  - id: implement
    type: agent
    role: builder
    mode: headless
    instruction: Cook exactly what the order says.
    inputs:
      - name: order
        from:
          graph_input: order
    outputs:
      - name: result
        schema:
          ref: result-schema
          revision: {order_schema}
  - id: approve
    type: wait
    prompt: Approve this candidate, or name the blocking defect.
    depends_on: [implement]
    outputs:
      - name: approval
        schema:
          ref: approval-schema
          revision: {approval_schema}
  - id: review
    type: agent
    role: builder
    mode: headless
    instruction: Check what the person approved.
    depends_on: [approve]
    outputs:
      - name: verdict
        schema:
          ref: verdict-schema
          revision: {order_schema}
""".encode()


@pytest.fixture
def runtime(tmp_path: Path, dbos_logging_isolation: None) -> Iterator[DbosRuntime]:
    """A runtime whose agents succeed, so the line reaches the person and ends."""

    started = DbosRuntime(
        canonical_runtime_settings(
            tmp_path, "round-trip-test", agent_scratch_root(tmp_path)
        ),
        canonical_loopback_effects(tmp_path),
        (
            RecordingAgentExecutorFactoryV2(
                "exact", "exact/v1", "exact-operation", PROVIDER_OUTPUT
            ),
        ),
    )
    started.initialize_storage()
    try:
        yield started
    finally:
        started.close()


def carried(answer: Mapping[str, Any], *names: str) -> dict[str, Any]:
    """The named fields of one answer, under the names that answer gave them.

    This is the whole mechanism of this file. It cannot produce a request field
    whose name the previous answer did not already use, so a consumer that had
    to translate one spelling into another could not write its next request
    through here at all -- it would fail here, naming the field that drifted.
    """

    absent = tuple(name for name in names if name not in answer)
    if absent:
        raise AssertionError(
            f"the answer does not name {absent}; it names {tuple(sorted(answer))}"
        )
    return {name: answer[name] for name in names}


def catalogued(answer: Mapping[str, Any], name: str) -> dict[str, Any]:
    """The one crossing on this wire that does rename a value, named once here.

    The catalog admits every kind through one door (#1172), so its body cannot
    spell the hash the way the publish door of each kind spells it: a workflow
    is published as `workflow_revision_hash`, an agent definition as
    `agent_definition_revision_hash`, and both are admitted as
    `catalog_revision_hash`. The alternative is one request body per kind, which
    is the parallel copy that door family exists to remove. Everything else in
    these round-trips still goes through `carried`, so this stays the one
    exception rather than the first of many.
    """

    return {"catalog_revision_hash": carried(answer, name)[name]}


def answered(response: Response, *expected: int) -> dict[str, Any]:
    assert response.status_code in expected, response.text
    body = response.json()
    assert isinstance(body, dict)
    return body


def read_until(client: TestClient, reference: str, state: RunState) -> dict[str, Any]:
    """The run as the consumer sees it once it reaches the state it waits for."""

    deadline = time.monotonic() + READ_DEADLINE_SECONDS
    run: dict[str, Any] = {}
    while time.monotonic() < deadline:
        run = answered(client.get(f"{RUN_PATH}/{reference}"), 200)
        if run["state"] == state.value:
            return run
        time.sleep(READ_INTERVAL_SECONDS)
    raise AssertionError(f"run stayed {run.get('state')!r}, expected {state.value!r}")


def node_owing_a_move(run: Mapping[str, Any]) -> Mapping[str, Any]:
    """The rail entry of the node this run says a person owes a move to."""

    owing = [
        node for node in run["node_rail"] if node["state"] == NodeState.NEEDS_YOU.value
    ]
    assert len(owing) == 1, run["node_rail"]
    return owing[0]


def model_configuration_revision_counts(runtime: DbosRuntime) -> tuple[int, int]:
    with runtime.engine.connect() as connection:
        return (
            int(
                connection.scalar(
                    sa.select(sa.func.count()).select_from(
                        host_model_registry_revisions
                    )
                )
                or 0
            ),
            int(
                connection.scalar(
                    sa.select(sa.func.count()).select_from(
                        host_project_model_defaults_revisions
                    )
                )
                or 0
            ),
        )


def configured_model_api(
    runtime: DbosRuntime,
    project_root: Path,
    model_registry_discoverer: ProviderModelDiscoverer | None = None,
    model_registry_validator: ProviderModelValidator | None = None,
) -> tuple[TestClient, str, dict[str, Any]]:
    project = ProjectId("model-configuration-contract")
    publish_project_root_revision(
        runtime.engine, ProjectRootRevision(project, 1, project_root)
    )
    client = durable_api_client(
        runtime,
        served_project_id=project,
        model_registry_discoverer=model_registry_discoverer,
        model_registry_validator=model_registry_validator,
    )
    auth_profile = answered(
        client.post(
            AUTH_PROFILE_PATH,
            json={
                "profile_id": "model-configuration",
                "revision_number": 1,
                "provider_id": "exact",
                "auth_mode": "subscription",
            },
        ),
        201,
    )
    configuration = answered(
        client.post(
            AGENT_CONFIGURATION_PATH,
            json={
                "model": "opus",
                "executor_revision": "exact/v1",
                **carried(auth_profile, "auth_profile_revision_hash"),
            },
        ),
        201,
    )
    return client, encode_public_project_reference(project), configuration


class FirstUseProviderModels:
    def __init__(self, validation: ProviderModelCheck) -> None:
        self.validation = validation
        self.validation_calls = 0

    def discover_models(
        self,
        configuration: AgentConfigurationRevision,
        auth_profile: AuthProfileRevision,
    ) -> ProviderModelDiscoveryResult:
        del configuration, auth_profile
        return ProviderModelDiscoveryUnsupported()

    def validate_model(
        self,
        configuration: AgentConfigurationRevision,
        auth_profile: AuthProfileRevision,
    ) -> ProviderModelValidationResult:
        del configuration, auth_profile
        self.validation_calls += 1
        return self.validation


@pytest.mark.proves("a-consumer-writes-every-request-out-of-the-answers-it-read")
def test_a_consumer_drives_four_round_trips_renaming_only_the_catalog_hash(
    runtime: DbosRuntime,
) -> None:
    """Name to start, run to answer, catalog to start, artifact to order.

    Read as a story: this consumer publishes what its document pins, publishes
    the document, names it, asks the catalog what that name holds, asks the
    listing what format that revision is, publishes the material its order
    carries, starts the run, waits for the person, answers, and reads the ended
    run. Every field of every request above came out of `carried`, except the
    admission's `catalog_revision_hash`, which `catalogued` names as the one
    crossing this wire renames.
    """

    client = durable_api_client(runtime)
    json_media = {"content-type": "application/json"}

    order_schema = answered(
        client.post(SCHEMA_PATH, content=ANY_JSON, headers=json_media), 200, 201
    )
    approval_schema = answered(
        client.post(SCHEMA_PATH, content=ONLY_A_STRING, headers=json_media), 200, 201
    )
    document = workflow_document(
        carried(order_schema, "schema_revision_hash")["schema_revision_hash"],
        carried(approval_schema, "schema_revision_hash")["schema_revision_hash"],
    )

    published = answered(
        client.post(
            WORKFLOW_PATH,
            content=document,
            headers={"content-type": "application/yaml"},
        ),
        201,
    )
    admitted = answered(
        client.post(
            LINEAGE_PATH,
            json={
                "kind": RevisionKind.WORKFLOW.value,
                **catalogued(published, "workflow_revision_hash"),
                "actor": "operator",
                "activated_at": "2026-08-18T00:00:00Z",
            },
        ),
        201,
    )

    # Round-trip 1: the name answers a revision, and the start writes it back.
    resolved = answered(client.get(f"{BY_NAME_PATH}/{admitted['display_name']}"), 200)
    assert resolved["catalog_revision_hash"] == published["workflow_revision_hash"]

    # Round-trip 3: the catalog listing answers the revision and its format,
    # which is exactly the pair a start has to state.
    listing = answered(
        client.get(WORKFLOW_PATH, params={"view": "described", "limit": "50"}), 200
    )
    listed = next(
        item
        for item in listing["items"]
        if item["workflow_revision_hash"]
        == carried(resolved, "catalog_revision_hash")["catalog_revision_hash"]
    )
    detail = answered(
        client.get(f"{WORKFLOW_PATH}/{listed['workflow_revision_hash']}"), 200
    )
    graph = detail["graph"]
    assert graph["workflow_format_version"] == listed["workflow_format_version"]

    auth_profile = answered(
        client.post(
            AUTH_PROFILE_PATH,
            json={
                "profile_id": "max",
                "revision_number": 1,
                "provider_id": "exact",
                "auth_mode": "subscription",
            },
        ),
        201,
    )
    configuration = answered(
        client.post(
            AGENT_CONFIGURATION_PATH,
            json={
                "model": "opus",
                "executor_revision": "exact/v1",
                **carried(auth_profile, "auth_profile_revision_hash"),
            },
        ),
        201,
    )
    registered = answered(
        client.put(
            MODEL_REGISTRY_PATH.replace("{provider_id}", "exact"),
            json={
                "revision_number": 1,
                "entries": [
                    {
                        "model_id": "opus",
                        **carried(configuration, "agent_configuration_revision_hash"),
                    }
                ],
            },
        ),
        201,
    )
    assert registered["provider_id"] == "exact"

    # Round-trip 4: material is published by its bytes and ordered by the
    # address that publication answered with.
    material = answered(
        client.post(
            ARTIFACT_PATH,
            content=ORDER_MATERIAL,
            headers={"content-type": "application/octet-stream"},
        ),
        201,
    )

    # The roles and the declared orders are lists of values rather than fields,
    # so they travel as values: the role the document binds, and the name the
    # document demands at start. The schema hull is the author's own
    # `schema: {ref, revision}` — carried, not rebuilt under other names.
    (role,) = graph["agent_roles"]
    (declared_order,) = graph["orders"]
    assert carried(declared_order["schema"], "ref", "revision") == {
        "ref": "order-schema",
        "revision": carried(order_schema, "schema_revision_hash")[
            "schema_revision_hash"
        ],
    }
    started = answered(
        client.post(
            RUN_PATH,
            json={
                "run_id": RUN_ID,
                **carried(listed, "workflow_revision_hash", "workflow_format_version"),
                "agent_bindings": [
                    {
                        "role": role,
                        **carried(configuration, "agent_configuration_revision_hash"),
                    }
                ],
                "orders": [
                    {
                        **carried(declared_order, "name"),
                        **carried(material, "artifact_hash"),
                    }
                ],
            },
        ),
        201,
    )

    runtime.launch()
    reference = carried(started, "public_run_reference")["public_run_reference"]
    waiting = read_until(client, reference, RunState.WAITING_INPUT)

    # Round-trip 2: the waiting run answers the revision it runs and the node
    # that owes a person a move. Its cancellation block publishes the exact
    # execution fence the answer writes back; the operator owns the attribution.
    target_execution = waiting["cancellation"]["target_node_execution_id"]
    assert isinstance(target_execution, str)
    accepted = answered(
        client.post(
            f"{RUN_PATH}/{reference}/answers",
            json={
                **carried(waiting, "workflow_revision_hash"),
                **carried(node_owing_a_move(waiting), "node_id"),
                "expected_node_execution_id": target_execution,
                "actor": "operator",
                "answer_base64": encode_canonical_base64(APPROVAL),
            },
        ),
        200,
        202,
    )
    assert accepted["run_id"] == started["run_id"]

    ended = read_until(client, reference, RunState.COMPLETED)
    assert ended["terminal_hash"] is not None
    assert [node["state"] for node in ended["node_rail"]] == [
        NodeState.SUCCEEDED.value
    ] * 3


@pytest.mark.parametrize(
    ("configuration_hash", "model_id"),
    (
        pytest.param("0" * 64, "opus", id="missing-configuration"),
        pytest.param(None, "other", id="mismatched-model"),
    ),
)
def test_model_registry_refuses_missing_or_mismatched_configuration_without_a_write(
    runtime: DbosRuntime,
    tmp_path: Path,
    configuration_hash: str | None,
    model_id: str,
) -> None:
    client, _project_reference, configuration = configured_model_api(runtime, tmp_path)
    before = model_configuration_revision_counts(runtime)

    response = client.put(
        MODEL_REGISTRY_PATH.replace("{provider_id}", "exact"),
        json={
            "revision_number": 1,
            "entries": [
                {
                    "model_id": model_id,
                    "agent_configuration_revision_hash": (
                        configuration["agent_configuration_revision_hash"]
                        if configuration_hash is None
                        else configuration_hash
                    ),
                }
            ],
        },
    )

    assert response.status_code == 422
    assert response.json()["type"].endswith(":invalid-request")
    assert model_configuration_revision_counts(runtime) == before


def test_a_registry_holds_one_model_under_two_configurations_but_not_one_twice(
    runtime: DbosRuntime, tmp_path: Path
) -> None:
    client, _project_reference, headless = configured_model_api(runtime, tmp_path)
    tooled = answered(
        client.post(
            AGENT_CONFIGURATION_PATH,
            json={
                **carried(
                    headless, "model", "executor_revision", "auth_profile_revision_hash"
                ),
                "requested_capability": "headless_with_tools",
            },
        ),
        201,
    )
    registry_path = MODEL_REGISTRY_PATH.replace("{provider_id}", "exact")
    both = [
        {
            "model_id": "opus",
            **carried(configuration, "agent_configuration_revision_hash"),
        }
        for configuration in (tooled, headless)
    ]

    registered = answered(
        client.put(registry_path, json={"revision_number": 1, "entries": both}),
        201,
    )
    before = model_configuration_revision_counts(runtime)
    repeated = client.put(
        registry_path, json={"revision_number": 2, "entries": [*both, both[0]]}
    )

    assert [
        (entry["model_id"], entry["agent_configuration_revision_hash"])
        for entry in registered["entries"]
    ] == sorted(
        (entry["model_id"], entry["agent_configuration_revision_hash"])
        for entry in both
    )
    assert repeated.status_code == 422
    assert repeated.json()["type"].endswith(":invalid-request")
    assert model_configuration_revision_counts(runtime) == before


@pytest.mark.parametrize(
    ("validation", "expected_check", "defaults_status"),
    (
        (ProviderModelCheck.CHECKED, "checked", 201),
        (ProviderModelCheck.UNKNOWN_AT_PROVIDER, "unknown-at-provider", 422),
    ),
)
def test_operator_model_requires_a_server_validation_revision_before_use(
    runtime: DbosRuntime,
    tmp_path: Path,
    validation: ProviderModelCheck,
    expected_check: str,
    defaults_status: int,
) -> None:
    provider_models = FirstUseProviderModels(validation)
    client, project_reference, configuration = configured_model_api(
        runtime,
        tmp_path,
        model_registry_discoverer=provider_models,
        model_registry_validator=provider_models,
    )
    configuration_hash = configuration["agent_configuration_revision_hash"]
    registry_path = MODEL_REGISTRY_PATH.replace("{provider_id}", "exact")
    first = answered(
        client.put(
            registry_path,
            json={
                "revision_number": 1,
                "entries": [
                    {
                        "model_id": "opus",
                        "agent_configuration_revision_hash": configuration_hash,
                    }
                ],
            },
        ),
        201,
    )
    assert first["entries"] == [
        {
            "model_id": "opus",
            "agent_configuration_revision_hash": configuration_hash,
            "source": "operator",
            "provider_check": "not-checked",
        }
    ]

    forged = client.put(
        registry_path,
        json={
            "revision_number": 2,
            "entries": [
                {
                    "model_id": "opus",
                    "agent_configuration_revision_hash": configuration_hash,
                    "source": "discovered",
                    "provider_check": "checked",
                }
            ],
        },
    )
    assert forged.status_code == 422

    defaults_path = PROJECT_MODEL_DEFAULTS_PATH.replace(
        "{public_project_reference}", project_reference
    )
    unchecked_default = {
        "difficulty": 2,
        "model_registry_revision_hash": first["model_registry_revision_hash"],
        "provider_id": "exact",
        "model_id": "opus",
        "agent_configuration_revision_hash": configuration_hash,
    }
    assert (
        client.put(
            defaults_path,
            json={"revision_number": 1, "defaults": [unchecked_default]},
        ).status_code
        == 422
    )

    validated = answered(
        client.post(
            registry_path + "/validations",
            json={"agent_configuration_revision_hash": configuration_hash},
        ),
        201,
    )
    assert validated["revision_number"] == 2
    assert validated["entries"][0]["provider_check"] == expected_check
    assert provider_models.validation_calls == 1
    retry = answered(
        client.post(
            registry_path + "/validations",
            json={"agent_configuration_revision_hash": configuration_hash},
        ),
        200,
    )
    assert retry == validated
    assert provider_models.validation_calls == 1

    checked_default = {
        **unchecked_default,
        "model_registry_revision_hash": validated["model_registry_revision_hash"],
    }
    response = client.put(
        defaults_path,
        json={"revision_number": 1, "defaults": [checked_default]},
    )
    assert response.status_code == defaults_status, response.text


@pytest.mark.parametrize("tuple_kind", ("missing", "mismatched", "foreign"))
def test_project_defaults_refuse_invalid_registry_tuples_without_a_write(
    runtime: DbosRuntime, tmp_path: Path, tuple_kind: str
) -> None:
    client, project_reference, configuration = configured_model_api(runtime, tmp_path)
    entry = {
        "model_id": "opus",
        "agent_configuration_revision_hash": configuration[
            "agent_configuration_revision_hash"
        ],
    }
    answered(
        client.put(
            MODEL_REGISTRY_PATH.replace("{provider_id}", "exact"),
            json={"revision_number": 1, "entries": [entry]},
        ),
        201,
    )
    current = answered(
        client.put(
            MODEL_REGISTRY_PATH.replace("{provider_id}", "exact"),
            json={"revision_number": 2, "entries": [entry]},
        ),
        201,
    )
    default = {
        "difficulty": 2,
        "model_registry_revision_hash": current["model_registry_revision_hash"],
        "provider_id": "exact",
        "model_id": "opus",
        "agent_configuration_revision_hash": configuration[
            "agent_configuration_revision_hash"
        ],
    }
    if tuple_kind == "missing":
        default["model_registry_revision_hash"] = "0" * 64
    elif tuple_kind == "mismatched":
        default["model_id"] = "other"
    elif tuple_kind == "foreign":
        default["provider_id"] = "foreign"
    else:
        raise AssertionError(f"unknown tuple kind {tuple_kind!r}")
    before = model_configuration_revision_counts(runtime)

    response = client.put(
        PROJECT_MODEL_DEFAULTS_PATH.replace(
            "{public_project_reference}", project_reference
        ),
        json={"revision_number": 1, "defaults": [default]},
    )

    assert response.status_code == 422
    assert response.json()["type"].endswith(":invalid-request")
    assert model_configuration_revision_counts(runtime) == before


def test_a_saved_default_survives_a_registry_append_when_setting_its_sibling(
    runtime: DbosRuntime, tmp_path: Path
) -> None:
    client, project_reference, configuration = configured_model_api(runtime, tmp_path)
    registry_path = MODEL_REGISTRY_PATH.replace("{provider_id}", "exact")
    first_registry = answered(
        client.put(
            registry_path,
            json={
                "revision_number": 1,
                "entries": [
                    {
                        "model_id": "opus",
                        **carried(configuration, "agent_configuration_revision_hash"),
                    }
                ],
            },
        ),
        201,
    )
    difficulty_three = {
        "difficulty": 3,
        "model_registry_revision_hash": first_registry["model_registry_revision_hash"],
        "provider_id": "exact",
        "model_id": "opus",
        **carried(configuration, "agent_configuration_revision_hash"),
    }
    defaults_path = PROJECT_MODEL_DEFAULTS_PATH.replace(
        "{public_project_reference}", project_reference
    )
    answered(
        client.put(
            defaults_path,
            json={"revision_number": 1, "defaults": [difficulty_three]},
        ),
        201,
    )
    sibling = answered(
        client.post(
            AGENT_CONFIGURATION_PATH,
            json={
                "model": "sonnet",
                "executor_revision": "exact/v1",
                **carried(configuration, "auth_profile_revision_hash"),
            },
        ),
        201,
    )
    latest_registry = answered(
        client.put(
            registry_path,
            json={
                "revision_number": 2,
                "entries": [
                    {
                        "model_id": "opus",
                        **carried(configuration, "agent_configuration_revision_hash"),
                    },
                    {
                        "model_id": "sonnet",
                        **carried(sibling, "agent_configuration_revision_hash"),
                    },
                ],
            },
        ),
        201,
    )
    difficulty_two = {
        "difficulty": 2,
        "model_registry_revision_hash": latest_registry["model_registry_revision_hash"],
        "provider_id": "exact",
        "model_id": "sonnet",
        **carried(sibling, "agent_configuration_revision_hash"),
    }

    stored = answered(
        client.put(
            defaults_path,
            json={
                "revision_number": 2,
                "defaults": [difficulty_three, difficulty_two],
            },
        ),
        201,
    )

    assert stored["defaults"] == [difficulty_two, difficulty_three]
    assert answered(client.get(defaults_path), 200) == stored
