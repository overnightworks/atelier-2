"""The operator's import door, driven through the composed server over a real store.

The acceptance of #652: after one import every open issue of the connected
repository is exactly one OBSERVED row in the unified projection; a repeated
import adds nothing; and an imported item can be proposed and confirmed.
GitHub is the same `httpx.MockTransport` stand-in the
observation source's own tests use -- no test reaches the real network.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from atelier2.adapters.dbos.runtime import DbosRuntime, DbosRuntimeSettings
from atelier2.adapters.github.composition import live_github_issue_source
from atelier2.adapters.github.observation import LiveGitHubIssueSource
from atelier2.adapters.loopback import LoopbackEffectAdapterFactory
from atelier2.api.openapi import (
    API_PREFIX,
    CATALOG_LINEAGES_PATH,
    PROJECT_QUEUE_POLICY_PATH,
    PROJECT_SOURCE_IMPORT_PATH,
    QUEUE_ADMISSIONS_PATH,
    QUEUE_ITEMS_PATH,
    QUEUE_PROPOSALS_PATH,
)
from atelier2.api.references import encode_public_project_reference
from atelier2.contracts.effects import AdapterRevision, EffectDestination
from atelier2.contracts.host_configuration import (
    ConnectionActor,
    ProjectId,
    ProjectSourceConnectionLifecycle,
    ProjectSourceConnectionRevision,
    ProjectSourceId,
    SourceAddress,
    SourceConnectionAuthMethod,
    SourceKind,
    SourceReference,
)
from atelier2.contracts.queue_projection import MAXIMUM_QUEUE_ITEM_TITLE_CHARACTERS
from atelier2.contracts.revisions_v3 import RevisionKind
from atelier2.contracts.when import recorded_instant
from tests.scenarios.api import durable_api_client

PROJECT = ProjectId("studio")
OWNER = "atelier2-operator"
REPO = "atelier2-target"
READ_INSTANT = recorded_instant(datetime(2026, 9, 4, 13, 8, 32, tzinfo=UTC))
WORKFLOW_DOCUMENT = b"""format_version: 3
name: triage-backlog
nodes:
  - id: review
    type: agent
    role: reviewer
    mode: headless
    instruction: Review one bounded diff.
"""


class _FakeGitHubIssueListing:
    def __init__(self, issues: list[dict[str, Any]], status: int = 200) -> None:
        self.issues = issues
        self.status = status

    def handle(self, request: httpx.Request) -> httpx.Response:
        if self.status != 200:
            return httpx.Response(self.status, json={"message": "refused"})
        per_page = int(request.url.params.get("per_page", "30"))
        page = int(request.url.params.get("page", "1"))
        start = (page - 1) * per_page
        return httpx.Response(200, json=self.issues[start : start + per_page])


@pytest.fixture
def runtime(tmp_path: Path) -> Iterator[DbosRuntime]:
    started = DbosRuntime(
        DbosRuntimeSettings(tmp_path / "atelier.sqlite", "queue-import-route-test"),
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


def issue_source(
    tmp_path: Path, listing: _FakeGitHubIssueListing
) -> LiveGitHubIssueSource:
    """The source exactly as serve composes it: from the connection record.

    The reading clock is fixed as well as the transport, because the import
    re-stamps every observed title with the listing's read instant: against the
    real clock a repeated import would differ from the first one whenever the
    two land in different wall-clock seconds.
    """

    credential_directory = tmp_path / "github-credential"
    credential_directory.mkdir(exist_ok=True)
    (credential_directory / "token").write_text("gho_test_token", encoding="utf-8")
    composed = live_github_issue_source(
        ProjectSourceConnectionRevision(
            PROJECT,
            ProjectSourceId("11111111-1111-1111-1111-111111111111"),
            1,
            SourceKind("github"),
            SourceAddress(f"{OWNER}/{REPO}"),
            credential_directory,
            SourceConnectionAuthMethod.PERSONAL_ACCESS_TOKEN,
            ConnectionActor("felix"),
            ProjectSourceConnectionLifecycle.CONNECTED,
            None,
            SourceReference("main"),
        )
    )
    return replace(
        composed,
        transport=httpx.MockTransport(listing.handle),
        clock=lambda: READ_INSTANT,
    )


def connected_api(
    runtime: DbosRuntime, tmp_path: Path, listing: _FakeGitHubIssueListing
) -> TestClient:
    return durable_api_client(
        runtime,
        served_project_id=PROJECT,
        tracker_item_source=issue_source(tmp_path, listing),
    )


def founded_workflow_lineage(api: TestClient) -> str:
    published = api.post(
        API_PREFIX + "/workflow-revisions",
        content=WORKFLOW_DOCUMENT,
        headers={"content-type": "application/yaml"},
    )
    assert published.status_code == 201, published.text
    founded = api.post(
        CATALOG_LINEAGES_PATH,
        json={
            "kind": RevisionKind.WORKFLOW.value,
            "catalog_revision_hash": published.json()["workflow_revision_hash"],
            "actor": "operator",
            "activated_at": "2026-08-27T00:00:00Z",
        },
    )
    assert founded.status_code == 201, founded.text
    return str(founded.json()["lineage_id"])


def test_importing_turns_every_open_issue_into_exactly_one_listable_observed_row(
    runtime: DbosRuntime, tmp_path: Path
) -> None:
    api = connected_api(
        runtime,
        tmp_path,
        _FakeGitHubIssueListing(
            [
                {"number": 79, "title": "Fix the flaky importer test"},
                {"number": 652, "title": "Add retry to the connection check"},
            ]
        ),
    )

    imported = api.post(PROJECT_SOURCE_IMPORT_PATH)

    assert imported.status_code == 200, imported.text
    assert imported.json() == {"observed": 2, "newly_observed": 2, "skipped": []}
    listed = api.get(QUEUE_ITEMS_PATH)
    assert listed.status_code == 200, listed.text
    page = listed.json()
    assert page["next_after"] is None
    assert {item["tracker_item_reference"] for item in page["items"]} == {
        "gh:79",
        "gh:652",
    }
    assert all(
        item["project_id"] == "studio"
        and item["revision"] == 0
        and len(item["item_id"]) == 64
        for item in page["items"]
    )


def test_a_repeated_import_adds_nothing(runtime: DbosRuntime, tmp_path: Path) -> None:
    api = connected_api(
        runtime,
        tmp_path,
        _FakeGitHubIssueListing(
            [
                {"number": 79, "title": "Fix the flaky importer test"},
                {"number": 652, "title": "Add retry to the connection check"},
            ]
        ),
    )
    assert api.post(PROJECT_SOURCE_IMPORT_PATH).status_code == 200
    first_list = api.get(QUEUE_ITEMS_PATH).json()

    repeated = api.post(PROJECT_SOURCE_IMPORT_PATH)

    assert repeated.status_code == 200, repeated.text
    assert repeated.json() == {"observed": 2, "newly_observed": 0, "skipped": []}
    assert api.get(QUEUE_ITEMS_PATH).json() == first_list


def test_an_imported_item_is_proposed_then_confirmed(
    runtime: DbosRuntime, tmp_path: Path
) -> None:
    api = connected_api(
        runtime,
        tmp_path,
        _FakeGitHubIssueListing(
            [{"number": 79, "title": "Fix the flaky importer test"}]
        ),
    )
    assert api.post(PROJECT_SOURCE_IMPORT_PATH).status_code == 200
    (observed,) = api.get(QUEUE_ITEMS_PATH).json()["items"]
    lineage_id = founded_workflow_lineage(api)

    policy_path = PROJECT_QUEUE_POLICY_PATH.replace(
        "{public_project_reference}", encode_public_project_reference(PROJECT)
    )
    policy = api.put(
        policy_path,
        json={
            "revision_number": 1,
            "expected_revision": 0,
            "maximum_active_runs": 1,
            "automation_label": None,
        },
    )
    assert policy.status_code == 201, policy.text
    proposal = api.put(
        QUEUE_PROPOSALS_PATH,
        json={
            "project_id": observed["project_id"],
            "tracker_item_reference": observed["tracker_item_reference"],
            "expected_revision": observed["revision"],
            "priority": {"rank": 1},
            "workflow_lineage_id": lineage_id,
            "prerequisite_item_ids": [],
            "automation_disposition": "HUMAN_REQUIRED",
            "policy_revision": 1,
        },
    )
    assert proposal.status_code == 201, proposal.text

    admitted = api.post(
        QUEUE_ADMISSIONS_PATH,
        json={
            "project_id": observed["project_id"],
            "tracker_item_reference": observed["tracker_item_reference"],
            "rationale": "matches the triage rule",
            "expected_revision": proposal.json()["revision"],
        },
    )

    assert admitted.status_code == 201, admitted.text
    assert admitted.json()["item_id"] == observed["item_id"]
    (admitted_item,) = api.get(QUEUE_ITEMS_PATH).json()["items"]
    assert admitted_item["item_id"] == observed["item_id"]
    assert admitted_item["state"] == "ADMITTED"


def test_importing_on_an_unconnected_instance_is_refused_by_name(
    runtime: DbosRuntime,
) -> None:
    api = durable_api_client(runtime)

    refused = api.post(PROJECT_SOURCE_IMPORT_PATH)

    assert refused.status_code == 409, refused.text
    assert refused.json()["type"].endswith(":project-source-not-connected")


def test_an_unanswering_platform_is_a_named_unavailability_writing_nothing(
    runtime: DbosRuntime, tmp_path: Path
) -> None:
    api = connected_api(runtime, tmp_path, _FakeGitHubIssueListing([], status=503))

    refused = api.post(PROJECT_SOURCE_IMPORT_PATH)

    assert refused.status_code == 503, refused.text
    assert refused.json()["type"].endswith(":project-source-unavailable")
    assert api.get(QUEUE_ITEMS_PATH).json()["items"] == []


def test_a_malformed_platform_payload_is_a_named_refusal_writing_nothing(
    runtime: DbosRuntime, tmp_path: Path
) -> None:
    api = connected_api(
        runtime, tmp_path, _FakeGitHubIssueListing([{"title": "no number"}])
    )

    refused = api.post(PROJECT_SOURCE_IMPORT_PATH)

    assert refused.status_code == 502, refused.text
    assert refused.json()["type"].endswith(":project-source-payload-malformed")
    assert api.get(QUEUE_ITEMS_PATH).json()["items"] == []


def test_an_unusable_title_is_named_and_the_rest_is_imported(
    runtime: DbosRuntime, tmp_path: Path
) -> None:
    api = connected_api(
        runtime,
        tmp_path,
        _FakeGitHubIssueListing(
            [
                {"number": 79, "title": "Fix the flaky importer test"},
                {
                    "number": 1446,
                    "title": "x" * (MAXIMUM_QUEUE_ITEM_TITLE_CHARACTERS + 1),
                },
            ]
        ),
    )

    imported = api.post(PROJECT_SOURCE_IMPORT_PATH)

    assert imported.status_code == 200, imported.text
    body = imported.json()
    assert body["observed"] == 1
    assert body["newly_observed"] == 1
    (skipped,) = body["skipped"]
    assert skipped["tracker_item_reference"] == "gh:1446"
    assert str(MAXIMUM_QUEUE_ITEM_TITLE_CHARACTERS) in skipped["reason"]
    listed = api.get(QUEUE_ITEMS_PATH)
    assert listed.status_code == 200, listed.text
    (item,) = listed.json()["items"]
    assert item["tracker_item_reference"] == "gh:79"


def test_listing_queue_items_on_an_empty_queue_is_an_empty_page(
    runtime: DbosRuntime,
) -> None:
    listed = durable_api_client(runtime).get(QUEUE_ITEMS_PATH)

    assert listed.status_code == 200, listed.text
    assert listed.json() == {"items": [], "next_after": None}
