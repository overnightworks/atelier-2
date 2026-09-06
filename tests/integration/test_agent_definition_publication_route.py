"""The HTTP door kind `agent_definition` was missing: authored bytes in, hash out.

The authoring format (#66) could already parse, stability-check, and package a
Markdown agent definition, but no surface published it. These tests drive the
real route against the real store and read the published bytes back by their
hash — because the sentence this door is worth anything for is not "the bytes
were stored" but "the exact authored definition comes back out".
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from httpx import Response

from atelier2.adapters.dbos.catalog_store import DbosCatalogStore
from atelier2.adapters.dbos.runtime import DbosRuntime
from atelier2.adapters.markdown_agent_definitions import parse_agent_definition
from atelier2.api.app import create_app
from atelier2.api.openapi import API_PREFIX
from atelier2.contracts.agent_definitions import DeclaredTools
from atelier2.contracts.revisions_v3 import PublishedRevisionHash, RevisionKind
from atelier2.ports.durable_runs import DurableStateCorrupt
from atelier2.ports.published_revisions import (
    PublishedRevisionFound,
    PublishedRevisionMissing,
    PublishedRevisionsUnavailable,
)
from tests.scenarios.agents import agent_scratch_root
from tests.scenarios.api import (
    api_limits,
    api_ports,
    durable_api_client,
    event_poll_backoff,
)
from tests.scenarios.durable_state import (
    canonical_loopback_effects,
    canonical_runtime_settings,
)

DEFINITION_PATH = f"{API_PREFIX}/agent-definition-revisions"
THE_DEFINITION = (
    b"---\n"
    b"name: stage-name-witness\n"
    b"description: Watches the stage and names what it sees.\n"
    b"model: sonnet\n"
    b"tools: Read, Grep\n"
    b"---\n"
    b"\nYou watch the stage and name what you see.\n"
)


@pytest.fixture
def runtime(tmp_path: Path) -> Iterator[DbosRuntime]:
    started = DbosRuntime(
        canonical_runtime_settings(
            tmp_path, "agent-definition-door-test", agent_scratch_root(tmp_path)
        ),
        canonical_loopback_effects(tmp_path),
    )
    started.initialize_storage()
    try:
        yield started
    finally:
        started.close()


def publish(api: TestClient, document: bytes) -> Response:
    return api.post(
        DEFINITION_PATH, content=document, headers={"content-type": "text/markdown"}
    )


@pytest.mark.proves("the-published-revision-reconstructs-the-definition")
def test_a_published_definition_read_back_by_its_hash_is_the_identical_definition(
    runtime: DbosRuntime,
) -> None:
    api = durable_api_client(runtime)

    created = publish(api, THE_DEFINITION)
    retried = publish(api, THE_DEFINITION)

    assert (created.status_code, retried.status_code) == (201, 200)
    assert created.json() == retried.json()
    definition_hash = created.json()["agent_definition_revision_hash"]
    assert definition_hash == PublishedRevisionHash.of(THE_DEFINITION).value
    resolved = DbosCatalogStore(runtime.engine).resolve(
        RevisionKind.AGENT_DEFINITION, PublishedRevisionHash(definition_hash)
    )
    assert isinstance(resolved, PublishedRevisionFound)
    assert resolved.revision.kind is RevisionKind.AGENT_DEFINITION
    assert resolved.revision.document == THE_DEFINITION

    authored = parse_agent_definition(THE_DEFINITION)
    reconstructed = parse_agent_definition(resolved.revision.document)
    assert reconstructed == authored
    assert reconstructed.name == "stage-name-witness"
    assert reconstructed.description == "Watches the stage and names what it sees."
    assert reconstructed.model == "sonnet"
    assert isinstance(reconstructed.tools, DeclaredTools)
    assert tuple(name.value for name in reconstructed.tools.names) == ("Grep", "Read")
    assert (
        reconstructed.system_prompt == "\nYou watch the stage and name what you see.\n"
    )


def test_the_definition_door_answers_every_field_its_author_wrote(
    runtime: DbosRuntime,
) -> None:
    api = durable_api_client(runtime)
    definition_hash = publish(api, THE_DEFINITION).json()[
        "agent_definition_revision_hash"
    ]

    read = api.get(f"{DEFINITION_PATH}/{definition_hash}")

    assert read.status_code == 200
    assert read.json() == {
        "agent_definition_revision_hash": definition_hash,
        "name": "stage-name-witness",
        "description": "Watches the stage and names what it sees.",
        "model": "sonnet",
        "system_prompt": "\nYou watch the stage and name what you see.\n",
        "tools": ["Grep", "Read"],
    }


def test_the_definition_door_answers_an_unrestricted_definition_with_no_tools(
    runtime: DbosRuntime,
) -> None:
    api = durable_api_client(runtime)
    unrestricted = (
        b"---\n"
        b"name: open-handed-scribe\n"
        b"description: Writes with whatever the executor offers.\n"
        b"---\n"
        b"\nYou write.\n"
    )
    definition_hash = publish(api, unrestricted).json()[
        "agent_definition_revision_hash"
    ]

    read = api.get(f"{DEFINITION_PATH}/{definition_hash}")

    assert read.status_code == 200
    body = read.json()
    assert body["model"] is None
    assert body["tools"] is None


def test_the_definition_door_accepts_a_provider_native_key_it_does_not_model(
    runtime: DbosRuntime,
) -> None:
    """The gap this closes: `color: cyan` used to be a named refusal.

    A provider-native key a real agent file's own tool writes is not a guess
    for this door to make and refuse; it is carried through opaquely, and the
    published identity is still the exact bytes handed in.
    """

    document = (
        b"---\n"
        b"name: witness\n"
        b"description: Watches.\n"
        b"color: cyan\n"
        b"---\n"
        b"Body.\n"
    )

    created = publish(durable_api_client(runtime), document)

    assert created.status_code == 201
    assert created.json()["agent_definition_revision_hash"] == (
        PublishedRevisionHash.of(document).value
    )


def test_the_definition_door_refuses_a_hash_nothing_published(
    runtime: DbosRuntime,
) -> None:
    unpublished_hash = PublishedRevisionHash.of(THE_DEFINITION).value

    read = durable_api_client(runtime).get(f"{DEFINITION_PATH}/{unpublished_hash}")

    assert read.status_code == 404
    assert read.json()["type"].endswith(":agent-definition-revision-not-found")


def test_the_definition_door_refuses_a_hash_published_under_another_kind(
    runtime: DbosRuntime,
) -> None:
    """A schema and an agent definition may share nothing but a hash's shape."""

    api = durable_api_client(runtime)
    schema_hash = api.post(
        f"{API_PREFIX}/schema-revisions",
        content=b'{"type": "boolean"}',
        headers={"content-type": "application/json"},
    ).json()["schema_revision_hash"]

    read = api.get(f"{DEFINITION_PATH}/{schema_hash}")

    assert read.status_code == 404
    assert read.json()["type"].endswith(":agent-definition-revision-not-found")


def test_the_definition_door_refuses_a_malformed_hash(runtime: DbosRuntime) -> None:
    read = durable_api_client(runtime).get(f"{DEFINITION_PATH}/not-a-hash")

    assert read.status_code == 400
    assert read.json()["type"].endswith(":invalid-revision-hash")


@dataclass
class ScriptedResolverRegistry:
    """A route-level fake standing in for the durable registry's `resolve`.

    Publication never reaches it: every case below asks only what the read
    door's resolve answers, not what a write would do with it.
    """

    answer: object

    def publish_revision(self, revision: object) -> object:
        del revision
        raise AssertionError("the read-door matrix registry never publishes")

    def resolve(self, kind: object, revision_hash: object) -> object:
        del kind, revision_hash
        return self.answer


def _scripted_definition_client(registry_answer: object) -> TestClient:
    app = create_app(
        source_commit="commit",
        source_tree="tree",
        ports=api_ports(
            published_revision_registry=ScriptedResolverRegistry(registry_answer)
        ),
        limits=api_limits(),
        event_poll_backoff=event_poll_backoff(),
    )
    return TestClient(app)


def test_the_definition_door_answers_unavailable_rather_than_a_false_not_found() -> (
    None
):
    """The store did not say "no such revision" -- it could not answer, and
    telling a caller 404 would stop retrying a hash that may yet resolve."""

    client = _scripted_definition_client(
        PublishedRevisionsUnavailable("registry asleep")
    )

    read = client.get(f"{DEFINITION_PATH}/{'a' * 64}")

    assert read.status_code == 503
    assert read.json()["type"].endswith(":temporarily-unavailable")


def test_the_definition_door_answers_durable_state_corrupt_rather_than_not_found() -> (
    None
):
    client = _scripted_definition_client(DurableStateCorrupt())

    read = client.get(f"{DEFINITION_PATH}/{'a' * 64}")

    assert read.status_code == 500
    assert read.json()["type"].endswith(":durable-state-corrupt")


def test_the_catalog_lists_every_published_definition_by_its_authored_name(
    runtime: DbosRuntime,
) -> None:
    """What the catalog view needs: a name, because a hash recognises nobody.

    Publication answers only a hash, so a reader who never held one -- the
    operator opening the catalog -- could not see that anything was published
    at all until this read existed.
    """

    api = durable_api_client(runtime)
    unrestricted = (
        b"---\n"
        b"name: open-handed-scribe\n"
        b"description: Writes with whatever the executor offers.\n"
        b"---\n"
        b"\nYou write.\n"
    )
    publish(api, THE_DEFINITION)
    publish(api, unrestricted)

    listed = api.get(DEFINITION_PATH)

    assert listed.status_code == 200
    page = listed.json()
    assert page["next_after_revision_hash"] is None
    by_name = {item["name"]: item for item in page["items"]}
    assert set(by_name) == {"stage-name-witness", "open-handed-scribe"}
    # The listing stops at the name and the sentence beside it. What model the
    # file asks for and which tools it declares are one provider's runtime
    # contract, read from the revision itself -- a catalog row that re-served
    # them would be claiming they mean something outside that provider.
    assert by_name["stage-name-witness"] == {
        "agent_definition_revision_hash": PublishedRevisionHash.of(
            THE_DEFINITION
        ).value,
        "name": "stage-name-witness",
        "description": "Watches the stage and names what it sees.",
    }
    assert by_name["open-handed-scribe"] == {
        "agent_definition_revision_hash": PublishedRevisionHash.of(unrestricted).value,
        "name": "open-handed-scribe",
        "description": "Writes with whatever the executor offers.",
    }


def test_an_empty_catalog_lists_nothing_rather_than_refusing(
    runtime: DbosRuntime,
) -> None:
    listed = durable_api_client(runtime).get(DEFINITION_PATH)

    assert listed.status_code == 200
    assert listed.json() == {"items": [], "next_after_revision_hash": None}


def test_the_definition_list_pages_through_every_published_definition(
    runtime: DbosRuntime,
) -> None:
    api = durable_api_client(runtime)
    documents = tuple(
        THE_DEFINITION.replace(b"stage-name-witness", f"witness-{index}".encode())
        for index in range(3)
    )
    for document in documents:
        publish(api, document)

    first = api.get(DEFINITION_PATH, params={"limit": "2"}).json()
    second = api.get(
        DEFINITION_PATH,
        params={"limit": "2", "after_revision_hash": first["next_after_revision_hash"]},
    ).json()

    assert len(first["items"]) == 2
    assert second["next_after_revision_hash"] is None
    walked = tuple(
        item["agent_definition_revision_hash"]
        for item in (*first["items"], *second["items"])
    )
    assert set(walked) == {PublishedRevisionHash.of(one).value for one in documents}


def test_the_definition_list_refuses_a_cursor_that_is_not_a_revision_hash(
    runtime: DbosRuntime,
) -> None:
    refused = durable_api_client(runtime).get(
        DEFINITION_PATH, params={"after_revision_hash": "not-a-hash"}
    )

    assert refused.status_code == 400
    assert refused.json()["type"].endswith(":invalid-revision-hash")


@pytest.mark.proves("the-same-file-is-the-same-revision-identity")
def test_two_definitions_differing_only_in_prompt_are_two_distinct_revisions(
    runtime: DbosRuntime,
) -> None:
    """The gap this door closes: the prompt now reaches a durable identity.

    The agent-configuration revision could not tell two prompts apart, which
    `test_todays_catalog_revision_cannot_tell_two_prompts_apart` pinned until
    this change retired it: the definition bytes are the durable truth now.
    """

    api = durable_api_client(runtime)
    idling = THE_DEFINITION.replace(
        b"You watch the stage and name what you see.", b"You watch nothing."
    )

    watching_hash = publish(api, THE_DEFINITION).json()[
        "agent_definition_revision_hash"
    ]
    idling_hash = publish(api, idling).json()["agent_definition_revision_hash"]

    assert watching_hash != idling_hash


@pytest.mark.proves("missing-frontmatter-minimum-is-refused-unknown-keys-are-carried")
@pytest.mark.parametrize(
    ("document", "problem_code", "named_subject"),
    (
        pytest.param(
            b"You are an agent with no frontmatter.\n",
            "agent-definition-frontmatter-missing",
            None,
            id="frontmatter-missing",
        ),
        pytest.param(
            b"---\ndescription: A nameless agent.\n---\nBody.\n",
            "agent-definition-field-missing",
            "name",
            id="required-field-missing",
        ),
        pytest.param(
            b"---\nname: [unclosed\n---\nBody.\n",
            "agent-definition-frontmatter-unparsable",
            None,
            id="frontmatter-unparsable",
        ),
    ),
)
def test_a_refused_definition_is_named_by_its_own_reason(
    runtime: DbosRuntime,
    document: bytes,
    problem_code: str,
    named_subject: str | None,
) -> None:
    refused = publish(durable_api_client(runtime), document)

    assert refused.status_code == 422
    assert refused.json()["type"].endswith(f":{problem_code}")
    if named_subject is not None:
        assert named_subject in refused.json()["detail"]
    missing = DbosCatalogStore(runtime.engine).resolve(
        RevisionKind.AGENT_DEFINITION, PublishedRevisionHash.of(document)
    )
    assert isinstance(missing, PublishedRevisionMissing)


def test_a_definition_publication_refuses_the_wrong_media_type(
    runtime: DbosRuntime,
) -> None:
    refused = durable_api_client(runtime).post(
        DEFINITION_PATH,
        content=THE_DEFINITION,
        headers={"content-type": "application/json"},
    )

    assert refused.status_code == 415
    assert refused.json()["type"].endswith(":unsupported-media-type")
