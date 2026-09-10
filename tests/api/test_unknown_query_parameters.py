from __future__ import annotations

from http import HTTPStatus
from typing import Annotated

import pytest
from fastapi import APIRouter, Depends, FastAPI, Query
from fastapi.testclient import TestClient
from pydantic import BaseModel

from atelier2.api._support import reject_unsupported_query_contract
from atelier2.api.app import create_app
from atelier2.api.openapi import API_PREFIX
from atelier2.api.problems import PROBLEM_TYPE_PREFIX
from tests.scenarios.api import (
    OPENAPI_DOCUMENT_PATH,
    api_limits,
    api_ports,
    event_poll_backoff,
    frozen_document_paths,
    resolved_frozen_path,
)


class _ModelQueryProbe(BaseModel):
    """The unsupported shape one of the tests below declares a route with.

    Module-level, not nested in its test: this file postpones annotation
    evaluation (`from __future__ import annotations`), and a route's own
    `__globals__` cannot resolve a class defined inside the function that
    happens to register it.
    """

    after: str | None = None


async def _model_query_probe_dependency(
    filters: Annotated[_ModelQueryProbe, Query()],
) -> _ModelQueryProbe:
    """A nested sub-dependency, not the route's own signature (see the test
    below): the production shape every real route's `context` and `_media`
    parameters already use.
    """

    return filters


async def _validation_alias_probe_dependency(
    after: Annotated[str | None, Query(validation_alias="cursor")] = None,
) -> str | None:
    return after


# SSE routes hold the connection open once their dependencies resolve
# (`fastapi.sse.EventSourceResponse`), so a request that clears this guard
# would block on the stream rather than return -- they are proven not to
# demand this guard's own known parameters, since they declare none, by the
# dedicated SSE test modules instead of an acceptance check here.
STREAMING_GET_PATHS = frozenset(
    {
        API_PREFIX + "/runs/{public_ref}/events",
        API_PREFIX + "/events",
    }
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


@pytest.mark.parametrize(
    "path_template",
    frozen_document_paths("get") + (OPENAPI_DOCUMENT_PATH,),
)
def test_an_unknown_query_parameter_is_refused_on_every_get_route(
    path_template: str,
) -> None:
    response = _client().get(
        resolved_frozen_path(path_template), params={"__unexpected_probe__": "1"}
    )

    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    body = response.json()
    assert body["type"] == PROBLEM_TYPE_PREFIX + "invalid-request"
    assert body["invalid_fields"][0]["path"] == "query/__unexpected_probe__"


WRITE_OPERATIONS = tuple(
    (method, path)
    for method in ("post", "put", "delete")
    for path in frozen_document_paths(method)
)


@pytest.mark.parametrize(("method", "path_template"), WRITE_OPERATIONS)
def test_an_unknown_query_parameter_is_refused_on_every_write_route(
    method: str, path_template: str
) -> None:
    """The guard aborts before any body is read, writes included (#1501).

    `tests/integration/test_unknown_query_parameters.py` proves the durable
    consequence -- that nothing is written -- against a real store; this proves
    the HTTP-visible half against the fast, portless client every other case
    here uses.
    """

    response = _client().request(
        method,
        resolved_frozen_path(path_template),
        params={"__unexpected_probe__": "1"},
    )

    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    body = response.json()
    assert body["type"] == PROBLEM_TYPE_PREFIX + "invalid-request"
    assert body["invalid_fields"][0]["path"] == "query/__unexpected_probe__"


@pytest.mark.parametrize(
    "path_template",
    tuple(
        path for path in frozen_document_paths("get") if path not in STREAMING_GET_PATHS
    )
    + (OPENAPI_DOCUMENT_PATH,),
)
def test_no_extra_query_parameter_stays_a_valid_request_on_every_get_route(
    path_template: str,
) -> None:
    """Portless proof that the guard itself never over-triggers.

    `api_ports()` leaves every durable port unwired, so a route that reaches
    one answers 500 here -- that says nothing about this guard and is exactly
    what `tests/integration/test_unknown_query_parameters.py` exists to rule
    out with a real store instead.
    """

    response = _client().get(resolved_frozen_path(path_template))

    assert response.status_code != HTTPStatus.UNPROCESSABLE_ENTITY


def test_a_pydantic_model_query_parameter_nested_in_an_included_router_fails_app_construction() -> (
    None
):
    """`reject_unsupported_query_contract` catches the shape it cannot expand,
    registered the way every production route actually is.

    FastAPI lets a whole Pydantic model stand in for one `Query()` parameter,
    expanding its fields at the wire -- but records only the model's own
    parameter name in the route's dependant, which is not a name any caller
    ever sends. This declares it on a *nested* sub-dependency of a route
    installed through `APIRouter` + `include_router`, exactly how
    `_install_routers` installs every real route: FastAPI 0.141 wraps an
    included router in `_IncludedRouter`, so a walk over `app.routes` that
    does not resolve through it would see no production route at all, nested
    dependency or not. No route declares this today; this is the
    construction-time refusal the first one would meet instead of a silently
    wrong guard.
    """

    router = APIRouter()

    @router.get("/probe")
    async def _route(
        filters: Annotated[_ModelQueryProbe, Depends(_model_query_probe_dependency)],
    ) -> dict[str, str]:
        del filters
        return {}

    probe = FastAPI()
    probe.include_router(router)

    with pytest.raises(TypeError, match="Pydantic model"):
        reject_unsupported_query_contract(probe)


def test_a_validation_alias_query_parameter_nested_in_an_included_router_fails_app_construction() -> (
    None
):
    """A `validation_alias` that differs from `alias` reads under a name
    `reject_unknown_query_params` does not know to look for, declared on a
    nested sub-dependency of a route installed through `APIRouter` +
    `include_router` (see the model-query-parameter case above for why that
    registration shape is the one this must catch). No route declares this
    today; this is the construction-time refusal the first one would meet
    instead of a silently wrong guard.
    """

    router = APIRouter()

    @router.get("/probe")
    async def _route(
        after: Annotated[
            str | None, Depends(_validation_alias_probe_dependency)
        ] = None,
    ) -> dict[str, str]:
        del after
        return {}

    probe = FastAPI()
    probe.include_router(router)

    with pytest.raises(RuntimeError, match="validation_alias"):
        reject_unsupported_query_contract(probe)
