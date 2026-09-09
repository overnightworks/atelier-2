from __future__ import annotations

import re
from http import HTTPStatus

import pytest
from fastapi.testclient import TestClient

from atelier2.api.app import COCKPIT_HOME_PATH, create_app
from atelier2.api.context import ApiPorts
from atelier2.api.openapi import OPERATION_PROBLEMS
from atelier2.api.problem_vocabulary import PROBLEM_DEFINITIONS
from atelier2.api.problems import (
    PROBLEM_TYPE_PREFIX,
    problem_resource,
)
from atelier2.contracts.runs import RunId
from atelier2.contracts.when import RecordedAt
from atelier2.ports.run_queries import (
    GetRunResult,
    ListRunsResult,
)
from tests.scenarios.api import api_limits, api_ports, event_poll_backoff

URN_SAFE_PROBLEM_CODE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
FRAMEWORK_ERROR_CASES = (
    ("/missing", "get", 404, "route-not-found"),
    ("/atelier/api/v1/health", "post", 405, "method-not-allowed"),
    ("/atelier/api/v1/runs", "post", 415, "unsupported-media-type"),
    ("/atelier/api/v1/runs", "get", 422, "invalid-request"),
)


def empty_ports() -> ApiPorts:
    class UnusedRunQueries:
        def get_run(self, run_id: RunId) -> GetRunResult:
            del run_id
            raise AssertionError("invalid request reached run queries")

        def list_runs(
            self,
            after: RunId | None,
            limit: int,
            state: object = None,
        ) -> ListRunsResult:
            del after, limit
            raise AssertionError("invalid request reached run queries")

    return api_ports(run_queries=UnusedRunQueries())


@pytest.mark.parametrize("code", sorted(PROBLEM_DEFINITIONS))
def test_every_problem_code_renders_exactly_its_own_definition(code: str) -> None:
    definition = PROBLEM_DEFINITIONS[code]

    resource = problem_resource(code)

    assert resource.model_dump(exclude_none=True) == {
        "type": PROBLEM_TYPE_PREFIX + code,
        "title": definition.title,
        "status": definition.status,
        "detail": definition.detail,
    }


@pytest.mark.parametrize("code", sorted(PROBLEM_DEFINITIONS))
def test_every_problem_code_is_safe_inside_its_type_urn(code: str) -> None:
    assert URN_SAFE_PROBLEM_CODE.fullmatch(code)


@pytest.mark.parametrize("code", sorted(PROBLEM_DEFINITIONS))
def test_every_problem_names_an_error_status_and_an_operator_action(
    code: str,
) -> None:
    definition = PROBLEM_DEFINITIONS[code]
    status = HTTPStatus(definition.status)

    assert status.is_client_error or status.is_server_error
    assert definition.title.strip() == definition.title != ""
    assert definition.detail.endswith(".")


def test_every_problem_is_reachable_through_a_documented_route() -> None:
    documented = set[str]().union(*OPERATION_PROBLEMS.values())
    normalized_by_the_framework = {code for *_, code in FRAMEWORK_ERROR_CASES}

    assert documented <= set(PROBLEM_DEFINITIONS)
    assert set(PROBLEM_DEFINITIONS) == documented | normalized_by_the_framework


@pytest.mark.parametrize(("path", "method", "status", "code"), FRAMEWORK_ERROR_CASES)
def test_framework_and_media_errors_are_normalized(
    path: str, method: str, status: int, code: str
) -> None:
    client = TestClient(
        create_app(
            source_commit="commit",
            source_tree="tree",
            ports=empty_ports(),
            limits=api_limits(),
            event_poll_backoff=event_poll_backoff(),
        ),
        raise_server_exceptions=False,
    )
    response = (
        client.get(path + "?limit=false")
        if code == "invalid-request"
        else client.request(method, path)
    )

    assert response.status_code == status
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["type"] == PROBLEM_TYPE_PREFIX + code
    if code == "invalid-request":
        assert set(response.json()) == {
            "type",
            "title",
            "status",
            "detail",
            "invalid_fields",
        }
        assert response.json()["invalid_fields"]
        assert response.json()["invalid_fields"][0]["path"]
        assert response.json()["invalid_fields"][0]["reason"]
    else:
        assert set(response.json()) == {"type", "title", "status", "detail"}


@pytest.mark.parametrize(
    ("path", "status", "location", "problem_code"),
    (
        ("/", 307, COCKPIT_HOME_PATH, None),
        ("/nope", 404, None, "route-not-found"),
    ),
)
def test_the_bare_host_root_sends_the_operator_to_the_ui_and_an_unknown_path_stays_a_named_problem(
    path: str,
    status: int,
    location: str | None,
    problem_code: str | None,
) -> None:
    client = TestClient(
        create_app(
            source_commit="commit",
            source_tree="tree",
            ports=empty_ports(),
            limits=api_limits(),
            event_poll_backoff=event_poll_backoff(),
        ),
        follow_redirects=False,
        raise_server_exceptions=False,
    )
    response = client.get(path)

    assert response.status_code == status
    if location is not None:
        assert response.headers["location"] == location
        return
    assert problem_code is not None
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["type"] == PROBLEM_TYPE_PREFIX + problem_code
    assert set(response.json()) == {"type", "title", "status", "detail"}


def test_app_requires_both_source_identities_at_construction() -> None:
    with pytest.raises(ValueError, match="source_commit"):
        create_app(
            source_commit="",
            source_tree="tree",
            ports=empty_ports(),
            limits=api_limits(),
            event_poll_backoff=event_poll_backoff(),
        )
    with pytest.raises(ValueError, match="source_tree"):
        create_app(
            source_commit="commit",
            source_tree="",
            ports=empty_ports(),
            limits=api_limits(),
            event_poll_backoff=event_poll_backoff(),
        )


def test_health_returns_only_injected_source_identities() -> None:
    client = TestClient(
        create_app(
            source_commit="source-commit",
            source_tree="source-tree",
            ports=empty_ports(),
            limits=api_limits(),
            event_poll_backoff=event_poll_backoff(),
            boot_clock=lambda: RecordedAt("2026-08-31T08:00:00Z"),
        )
    )

    response = client.get("/atelier/api/v1/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "serving",
        "source_commit": "source-commit",
        "source_tree": "source-tree",
        "serve_started_at": "2026-08-31T08:00:00Z",
    }
