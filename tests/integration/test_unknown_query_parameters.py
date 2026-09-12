"""The unknown-query-parameter guard (#1501), proven against a real store.

`tests/api/test_unknown_query_parameters.py` proves the guard itself, fast and
portless. This module proves what only a real durable store can show: an
accepted request behind this guard is actually healthy (not merely "not
422"); a request this guard rejects reaches no write at all, across every
write operation the frozen document declares; and, for a representative
selection of those, that a row an otherwise-valid write would have changed
keeps its exact prior content, not merely its prior count.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine

from atelier2.adapters.dbos.queue_tables import queue_project_policy_revisions
from atelier2.adapters.dbos.runtime import DbosRuntimeSettings, create_canonical_engine
from atelier2.adapters.dbos.schema import (
    host_model_registry_revisions,
    initialize_schema,
)
from atelier2.adapters.dbos.table_vocabulary import metadata
from atelier2.api.app import create_app
from atelier2.api.openapi import (
    API_PREFIX,
    LIBRARY_ADDITION_PATH,
    MODEL_REGISTRY_PATH,
    PROJECT_QUEUE_POLICY_PATH,
)
from atelier2.api.problems import PROBLEM_TYPE_PREFIX
from atelier2.api.references import encode_public_project_reference
from atelier2.contracts.host_configuration import ProjectId
from atelier2.ports.agent_executions import AgentExecutorRegistry
from tests.scenarios.api import (
    OPENAPI_DOCUMENT_PATH,
    api_limits,
    durable_ports,
    event_poll_backoff,
    frozen_document_paths,
    resolved_frozen_path,
)

# See tests/api/test_unknown_query_parameters.py: these hold the connection
# open once their dependencies resolve, so a bare "known-only" request would
# block on the stream here rather than answer.
STREAMING_GET_PATHS = frozenset(
    {
        API_PREFIX + "/runs/{public_ref}/events",
        API_PREFIX + "/events",
    }
)

# GET /library/additions/{intake_id} answers 422 `invalid-request` for a
# well-formed id an empty store does not hold (`LibraryAdditionMissing`) --
# its own pre-existing contract, unrelated to #1501's guard. Every example
# id here is fabricated, so a real, empty store never holds it; the acceptance
# check below excludes only this one named route rather than weakening the
# "not 422" assertion every other route still meets.
ROUTES_WITH_THEIR_OWN_422 = frozenset({LIBRARY_ADDITION_PATH})


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    configured = create_canonical_engine(tmp_path / "atelier.sqlite")
    initialize_schema(configured)
    try:
        yield configured
    finally:
        configured.dispose()


def _client(engine: Engine) -> TestClient:
    """A real, empty durable store behind every port -- not `UnusedPort`.

    No agent executor is registered: every case here either never reaches a
    read that would need one, or is refused by the guard before any read at
    all, so an empty registry only says none of these cases depends on one.
    """

    return TestClient(
        create_app(
            source_commit="commit",
            source_tree="tree",
            ports=durable_ports(
                engine,
                DbosRuntimeSettings(
                    database_path=Path("unused"), application_version="test"
                ),
                AgentExecutorRegistry(()),
            ),
            limits=api_limits(),
            event_poll_backoff=event_poll_backoff(),
        ),
        raise_server_exceptions=False,
    )


def _durable_row_counts(engine: Engine) -> dict[str, int]:
    """Every durable table's row count, the cheapest whole-store "did this
    write anything" proof: the guard either aborts before any write is even
    attempted, or it does not run at all -- there is no shape of write this
    could miss by counting rows instead of reading their content.
    """

    with engine.connect() as connection:
        return {
            name: connection.execute(
                sa.select(sa.func.count()).select_from(table)
            ).scalar_one()
            for name, table in metadata.tables.items()
        }


@pytest.mark.parametrize(
    "path_template",
    tuple(
        path
        for path in frozen_document_paths("get")
        if path not in STREAMING_GET_PATHS and path not in ROUTES_WITH_THEIR_OWN_422
    )
    + (OPENAPI_DOCUMENT_PATH,),
)
def test_no_extra_query_parameter_stays_a_valid_request_on_every_get_route(
    engine: Engine, path_template: str
) -> None:
    response = _client(engine).get(resolved_frozen_path(path_template))

    assert response.status_code != 422
    assert response.status_code < 500


def test_the_one_route_with_its_own_422_still_answers_below_500(engine: Engine) -> None:
    """`LIBRARY_ADDITION_PATH` earns `ROUTES_WITH_THEIR_OWN_422`'s exclusion,
    not a free pass: a known-only request still never answers 500.
    """

    response = _client(engine).get(resolved_frozen_path(LIBRARY_ADDITION_PATH))

    assert response.status_code == 422
    assert response.status_code < 500


WRITE_OPERATIONS = tuple(
    (method, path)
    for method in ("post", "put", "delete")
    for path in frozen_document_paths(method)
)


@pytest.mark.parametrize(("method", "path_template"), WRITE_OPERATIONS)
def test_an_unknown_query_parameter_on_a_write_route_writes_nothing(
    engine: Engine, method: str, path_template: str
) -> None:
    before = _durable_row_counts(engine)

    response = _client(engine).request(
        method,
        resolved_frozen_path(path_template),
        params={"__unexpected_probe__": "1"},
    )

    assert response.status_code == 422
    assert response.json()["type"] == PROBLEM_TYPE_PREFIX + "invalid-request"
    assert _durable_row_counts(engine) == before


def _table_rows(engine: Engine, table: sa.Table) -> tuple[tuple[Any, ...], ...]:
    """Every row of one named table, as plain tuples: row count alone cannot
    tell an insert-shaped write from an update-shaped one that happens to
    leave the count unchanged, so the two tests below compare content.
    """

    with engine.connect() as connection:
        return tuple(sorted(tuple(row) for row in connection.execute(sa.select(table))))


def test_an_unknown_query_parameter_on_a_seeded_queue_policy_update_leaves_its_row_content_unchanged(
    engine: Engine,
) -> None:
    """A seeded, non-empty queue policy; an otherwise-valid update (correct
    body, correct CAS revision) that would change its stored row, carrying
    the unknown parameter, changes nothing -- proven by the row's exact
    content, not by `test_an_unknown_query_parameter_on_a_write_route_writes_nothing`'s
    whole-store row count.
    """

    client = _client(engine)
    path = PROJECT_QUEUE_POLICY_PATH.replace(
        "{public_project_reference}",
        encode_public_project_reference(ProjectId("studio")),
    )
    seeded = client.put(
        path,
        json={"revision_number": 1, "expected_revision": 0, "maximum_active_runs": 3},
    )
    assert seeded.status_code == 201, seeded.text
    before = _table_rows(engine, queue_project_policy_revisions)

    response = client.put(
        path,
        params={"__unexpected_probe__": "1"},
        json={"revision_number": 2, "expected_revision": 1, "maximum_active_runs": 9},
    )

    assert response.status_code == 422
    assert response.json()["type"] == PROBLEM_TYPE_PREFIX + "invalid-request"
    assert _table_rows(engine, queue_project_policy_revisions) == before
    assert client.get(path).json()["maximum_active_runs"] == 3


def test_an_unknown_query_parameter_on_a_seeded_model_registry_update_leaves_its_row_content_unchanged(
    engine: Engine,
) -> None:
    """A seeded, non-empty model registry; an otherwise-valid update (correct
    body, correct CAS revision) that would change its stored row, carrying
    the unknown parameter, changes nothing -- proven by the row's exact
    content, not by `test_an_unknown_query_parameter_on_a_write_route_writes_nothing`'s
    whole-store row count. Empty `entries` needs no agent-configuration
    revision seeded first, so this stays about the guard, not the registry.
    """

    client = _client(engine)
    path = MODEL_REGISTRY_PATH.replace("{provider_id}", "exact")
    seeded = client.put(path, json={"revision_number": 1, "entries": []})
    assert seeded.status_code == 201, seeded.text
    before = _table_rows(engine, host_model_registry_revisions)

    response = client.put(
        path,
        params={"__unexpected_probe__": "1"},
        json={"revision_number": 2, "entries": []},
    )

    assert response.status_code == 422
    assert response.json()["type"] == PROBLEM_TYPE_PREFIX + "invalid-request"
    assert _table_rows(engine, host_model_registry_revisions) == before
    assert client.get(path).json()["revision_number"] == 1
