from __future__ import annotations

import json
import re
from http import HTTPStatus
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from atelier2.api.app import create_app
from atelier2.api.openapi import API_PREFIX
from atelier2.api.problems import PROBLEM_TYPE_PREFIX
from tests.scenarios.api import api_limits, api_ports, event_poll_backoff

FROZEN_DOCUMENT_PATH = Path(__file__).with_name("openapi_frozen.json")

# SSE routes hold the connection open once their dependencies resolve
# (`fastapi.sse.EventSourceResponse`), so a request that clears this guard
# would block on the stream rather than return -- they stay in the rejection
# check below (which always short-circuits before the stream opens) and are
# proven not to demand this guard's own known parameters, since they declare
# none, by the dedicated SSE test modules instead.
STREAMING_GET_PATHS = frozenset(
    {
        API_PREFIX + "/runs/{public_ref}/events",
        API_PREFIX + "/events",
    }
)

# The one value per path-parameter name every GET route in the frozen
# document needs to reach its own business logic without tripping a route's
# *own* validation -- which would otherwise be indistinguishable, in a bare
# status code, from this guard's. `kind`, `provider_id`, and `intake_id` are
# the only names a route enforces before ever reaching this guard's sibling
# dependencies (a real path-level pattern, or a parse inside the endpoint);
# every other name here can be any short opaque string because the route
# itself turns a malformed one into a non-422 problem (400 or 404).
PATH_PARAMETER_EXAMPLES: dict[str, str] = {
    "artifact_hash": "a" * 64,
    "schema_revision_hash": "a" * 64,
    "agent_definition_revision_hash": "a" * 64,
    "workflow_revision_hash": "a" * 64,
    "intake_id": "a" * 64,
    "kind": "workflow",
    "name": "example",
    "public_project_reference": "example",
    "public_ref": "example",
    "node_id": "example",
    "provider_id": "example",
}

_PATH_PARAMETER = re.compile(r"\{([^}]+)\}")


def _resolved_path(template: str) -> str:
    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        assert name in PATH_PARAMETER_EXAMPLES, (
            f"no example value registered for path parameter {name!r}; "
            "add one to PATH_PARAMETER_EXAMPLES"
        )
        return PATH_PARAMETER_EXAMPLES[name]

    return _PATH_PARAMETER.sub(replace, template)


def _frozen_get_paths() -> tuple[str, ...]:
    document = json.loads(FROZEN_DOCUMENT_PATH.read_text())
    return tuple(
        path for path, operations in document["paths"].items() if "get" in operations
    )


def _client() -> TestClient:
    return TestClient(
        create_app(
            source_commit="commit",
            source_tree="tree",
            ports=api_ports(),
            limits=api_limits(),
            event_poll_backoff=event_poll_backoff(),
        ),
        raise_server_exceptions=False,
    )


def test_workflow_revisions_after_typo_names_the_typo_and_the_known_parameter() -> None:
    """The case #1485 hid for three weeks: `after` instead of `after_revision_hash`."""

    response = _client().get(API_PREFIX + "/workflow-revisions?after=x")

    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    body = response.json()
    assert body["type"] == PROBLEM_TYPE_PREFIX + "invalid-request"
    assert len(body["invalid_fields"]) == 1
    assert body["invalid_fields"][0]["path"] == "query/after"
    assert "after_revision_hash" in body["invalid_fields"][0]["reason"]


@pytest.mark.parametrize("path_template", _frozen_get_paths())
def test_an_unknown_query_parameter_is_refused_on_every_get_route(
    path_template: str,
) -> None:
    response = _client().get(
        _resolved_path(path_template), params={"__unexpected_probe__": "1"}
    )

    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    body = response.json()
    assert body["type"] == PROBLEM_TYPE_PREFIX + "invalid-request"
    assert body["invalid_fields"][0]["path"] == "query/__unexpected_probe__"


@pytest.mark.parametrize(
    "path_template",
    tuple(path for path in _frozen_get_paths() if path not in STREAMING_GET_PATHS),
)
def test_no_extra_query_parameter_stays_a_valid_request_on_every_get_route(
    path_template: str,
) -> None:
    response = _client().get(_resolved_path(path_template))

    assert response.status_code != HTTPStatus.UNPROCESSABLE_ENTITY
