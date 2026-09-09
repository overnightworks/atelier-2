"""The queue policy's read door: every application outcome, exact HTTP shape.

`tests/integration/test_queue_project_policy_read.py` proves the real
read-modify-write flow against durable storage; this module proves the route
maps every `QueuePolicyReader` answer -- found, absent, unavailable, corrupt
-- to its exact response, driven through the real HTTP door but without a
live store.
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi.testclient import TestClient

from atelier2.api.app import create_app
from atelier2.api.openapi import PROJECT_QUEUE_POLICY_PATH
from atelier2.api.references import encode_public_project_reference
from atelier2.contracts.catalog_v3 import CatalogLineageId
from atelier2.contracts.host_configuration import ProjectId
from atelier2.contracts.queue_projection import (
    QueueAutomationDisposition,
    QueuePriorityRank,
    QueueProjectPolicyDefaults,
    QueueProjectPolicyRevision,
)
from atelier2.ports.durable_runs import DurableStateCorrupt as PortDurableStateCorrupt
from atelier2.ports.queue_projection import (
    QueueProjectPolicyAbsent,
    QueueProjectPolicyFound,
    QueueReadUnavailable,
    ReadQueueProjectPolicyResult,
)
from tests.scenarios.api import api_limits, api_ports, event_poll_backoff

PROJECT = ProjectId("studio")


@dataclass
class PolicyReader:
    """A `QueuePolicyReader` double that answers one scripted result."""

    result: ReadQueueProjectPolicyResult

    def current_policy(self, project: ProjectId) -> ReadQueueProjectPolicyResult:
        assert project == PROJECT
        return self.result


def policy_path() -> str:
    return PROJECT_QUEUE_POLICY_PATH.replace(
        "{public_project_reference}", encode_public_project_reference(PROJECT)
    )


def client_reading(result: ReadQueueProjectPolicyResult) -> TestClient:
    return TestClient(
        create_app(
            source_commit="commit",
            source_tree="tree",
            ports=api_ports(queue_projection=PolicyReader(result)),
            limits=api_limits(),
            event_poll_backoff=event_poll_backoff(),
        )
    )


def test_reading_a_found_policy_answers_every_field_and_the_current_revision() -> None:
    policy = QueueProjectPolicyRevision(
        PROJECT,
        3,
        7,
        "bereit",
        QueueProjectPolicyDefaults(
            CatalogLineageId("a" * 64),
            QueuePriorityRank(1),
            QueueAutomationDisposition.AUTOMATION_AUTHORIZED,
        ),
    )
    client = client_reading(QueueProjectPolicyFound(policy))

    response = client.get(policy_path())

    assert response.status_code == 200, response.text
    assert response.json() == {
        "project_id": "studio",
        "revision_number": 3,
        "maximum_active_runs": 7,
        "automation_label": "bereit",
        "default_workflow_lineage_id": "a" * 64,
        "default_priority_rank": 1,
        "automation_disposition_default": "AUTOMATION_AUTHORIZED",
    }


def test_reading_an_absent_policy_is_refused_by_name() -> None:
    client = client_reading(QueueProjectPolicyAbsent())

    response = client.get(policy_path())

    assert response.status_code == 404, response.text
    assert response.json()["type"].endswith(":queue-policy-not-set")


def test_an_unanswering_store_is_a_named_unavailability() -> None:
    client = client_reading(QueueReadUnavailable())

    response = client.get(policy_path())

    assert response.status_code == 503, response.text
    assert response.json()["type"].endswith(":temporarily-unavailable")


def test_a_corrupt_store_is_a_named_refusal() -> None:
    client = client_reading(PortDurableStateCorrupt())

    response = client.get(policy_path())

    assert response.status_code == 500, response.text
    assert response.json()["type"].endswith(":durable-state-corrupt")
