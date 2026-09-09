"""The queue policy's read door, driven through the composed server.

The acceptance of #1463: `GET` on the policy path answers with every field and
the current revision from the same truth `PUT` already writes, so a caller can
read, change one field, and write back with `expected_revision` -- never
guessing a field the read already answered.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from atelier2.adapters.dbos.runtime import DbosRuntime, DbosRuntimeSettings
from atelier2.adapters.loopback import LoopbackEffectAdapterFactory
from atelier2.api.openapi import PROJECT_QUEUE_POLICY_PATH
from atelier2.api.references import encode_public_project_reference
from atelier2.contracts.effects import AdapterRevision, EffectDestination
from atelier2.contracts.host_configuration import ProjectId
from tests.scenarios.api import durable_api_client

PROJECT = ProjectId("studio")


@pytest.fixture
def runtime(tmp_path: Path) -> Iterator[DbosRuntime]:
    started = DbosRuntime(
        DbosRuntimeSettings(tmp_path / "atelier.sqlite", "queue-policy-read-test"),
        LoopbackEffectAdapterFactory(
            tmp_path / "external.sqlite",
            AdapterRevision("loopback-v1"),
            EffectDestination("loopback-test"),
        ),
    )
    started.initialize_storage()
    try:
        yield started
    finally:
        started.close()


def policy_path(project: ProjectId) -> str:
    return PROJECT_QUEUE_POLICY_PATH.replace(
        "{public_project_reference}", encode_public_project_reference(project)
    )


def test_reading_a_project_with_no_published_policy_is_refused_by_name(
    runtime: DbosRuntime,
) -> None:
    api = durable_api_client(runtime)

    refused = api.get(policy_path(PROJECT))

    assert refused.status_code == 404, refused.text
    assert refused.json()["type"].endswith(":queue-policy-not-set")


def test_reading_after_writing_answers_every_field_so_a_rewrite_invents_nothing(
    runtime: DbosRuntime,
) -> None:
    api: TestClient = durable_api_client(runtime)
    published = api.put(
        policy_path(PROJECT),
        json={
            "revision_number": 1,
            "expected_revision": 0,
            "maximum_active_runs": 2,
            "automation_label": "triage",
        },
    )
    assert published.status_code == 201, published.text

    read = api.get(policy_path(PROJECT))

    assert read.status_code == 200, read.text
    assert read.json() == published.json()

    current = read.json()
    rewritten = api.put(
        policy_path(PROJECT),
        json={
            "revision_number": current["revision_number"] + 1,
            "expected_revision": current["revision_number"],
            "maximum_active_runs": 5,
            "automation_label": current["automation_label"],
        },
    )

    assert rewritten.status_code == 201, rewritten.text
    reread = api.get(policy_path(PROJECT))
    assert reread.status_code == 200, reread.text
    assert reread.json() == {
        "project_id": PROJECT.value,
        "revision_number": 2,
        "maximum_active_runs": 5,
        "automation_label": "triage",
        "default_workflow_lineage_id": None,
        "default_priority_rank": None,
        "automation_disposition_default": None,
    }
