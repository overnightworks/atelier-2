"""The public intake door persists only the kind its caller declares."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

from atelier2.adapters.dbos.runtime import DbosRuntime, DbosRuntimeSettings
from atelier2.adapters.dbos.schema import catalog_intakes
from atelier2.adapters.loopback import LoopbackEffectAdapterFactory
from atelier2.api.openapi import LIBRARY_ADDITIONS_PATH
from atelier2.contracts.effects import AdapterRevision, EffectDestination
from tests.scenarios.api import durable_api_client


@pytest.fixture
def runtime(tmp_path: Path) -> Iterator[DbosRuntime]:
    started = DbosRuntime(
        DbosRuntimeSettings(tmp_path / "atelier.sqlite", "catalog-intake-test"),
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


def add(api: TestClient, document: bytes, kind: str):
    return api.post(
        LIBRARY_ADDITIONS_PATH,
        content=document,
        params={
            "kind": kind,
            "actor": "operator",
            "activated_at": "2026-08-27T00:00:00Z",
        },
        headers={"content-type": "application/octet-stream"},
    )


@pytest.mark.proves("a-catalog-intake-keeps-the-kind-it-was-handed-in")
def test_declared_kind_is_stored_and_read_back(runtime: DbosRuntime) -> None:
    added = add(durable_api_client(runtime), b"opaque", "workflow")
    assert added.status_code == 201, added.text
    assert added.json()["kind"] == "workflow"
    read = durable_api_client(runtime).get(
        f"{LIBRARY_ADDITIONS_PATH}/{added.json()['intake_id']}"
    )
    assert read.status_code == 200
    assert read.json() == added.json()


@pytest.mark.proves("a-catalog-intake-keeps-the-kind-it-was-handed-in")
def test_same_bytes_declared_under_two_kinds_are_two_intakes(
    runtime: DbosRuntime,
) -> None:
    api = durable_api_client(runtime)
    agent, skill = add(api, b"same bytes", "agent"), add(api, b"same bytes", "skill")
    assert agent.status_code == skill.status_code == 201
    assert agent.json()["intake_id"] != skill.json()["intake_id"]
    with runtime.engine.connect() as connection:
        assert (
            connection.scalar(sa.select(sa.func.count()).select_from(catalog_intakes))
            == 2
        )


@pytest.mark.proves("a-catalog-intake-keeps-the-kind-it-was-handed-in")
def test_unknown_kind_is_refused_without_a_durable_intake(runtime: DbosRuntime) -> None:
    refused = add(durable_api_client(runtime), b"opaque", "unknown")
    assert refused.status_code == 422
    assert refused.json()["type"].endswith(":invalid-request")
    with runtime.engine.connect() as connection:
        assert (
            connection.scalar(sa.select(sa.func.count()).select_from(catalog_intakes))
            == 0
        )


@pytest.mark.proves("a-catalog-intake-keeps-the-kind-it-was-handed-in")
def test_agent_looking_content_declared_as_skill_stays_skill(
    runtime: DbosRuntime,
) -> None:
    document = b"---\nname: looks-like-an-agent\ndescription: declared otherwise\n---\nPrompt\n"
    added = add(durable_api_client(runtime), document, "skill")
    assert added.status_code == 201
    assert added.json()["kind"] == "skill"


def test_catalog_intakes_are_immutable(runtime: DbosRuntime) -> None:
    added = add(durable_api_client(runtime), b"opaque", "workflow")
    intake_id = added.json()["intake_id"]
    with runtime.engine.begin() as connection, pytest.raises(IntegrityError):
        connection.execute(
            sa.update(catalog_intakes)
            .where(catalog_intakes.c.intake_id == intake_id)
            .values(kind="skill")
        )
    with runtime.engine.begin() as connection, pytest.raises(IntegrityError):
        connection.execute(
            sa.delete(catalog_intakes).where(catalog_intakes.c.intake_id == intake_id)
        )
