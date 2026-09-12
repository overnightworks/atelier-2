"""`publish_model_registry`'s inspection, carry-forward, and refusal branches.

A real `DbosHostConfigurationChannel`/`DbosAgentConfigurationCatalog` pair
drives this, matching `tests/integration/test_host_configuration.py`'s
convention for this port: the channel protocol is wide enough that a
hand-rolled double would drift from it, while the real adapter over a
throwaway sqlite file stays true to the store's own constraints.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest
from sqlalchemy.engine import Engine

from atelier2.adapters.dbos.agent_catalog import DbosAgentConfigurationCatalog
from atelier2.adapters.dbos.host_configuration import DbosHostConfigurationChannel
from atelier2.adapters.dbos.runtime import create_canonical_engine
from atelier2.adapters.dbos.schema import (
    agent_configuration_revisions,
    auth_profile_revisions,
    initialize_schema,
)
from atelier2.application.model_configuration import (
    ModelRegistryInvalid,
    ModelRegistryPublished,
    ModelRegistryUnchanged,
    publish_model_registry,
)
from atelier2.application.refusals import WriteUnavailable
from atelier2.contracts.agents import (
    AgentConfigurationRevision,
    AgentConfigurationRevisionFormatVersion,
    AgentExecutionCapability,
    AgentExecutorRevision,
    AuthMode,
    AuthProfileRevision,
    ProviderId,
)
from atelier2.contracts.host_configuration import (
    ModelRegistryEntrySource,
    ProviderModelCheck,
)
from atelier2.ports.agent_executions import AgentExecutorRegistry
from atelier2.ports.host_configuration import (
    ProviderModelDiscovery,
    ProviderModelInspectionUnavailable,
)


@pytest.fixture
def engine(tmp_path: Path) -> Engine:
    engine = create_canonical_engine(tmp_path / "atelier.sqlite")
    initialize_schema(engine)
    return engine


@pytest.fixture
def channel(engine: Engine) -> DbosHostConfigurationChannel:
    return DbosHostConfigurationChannel(engine)


@pytest.fixture
def catalog(engine: Engine) -> DbosAgentConfigurationCatalog:
    return DbosAgentConfigurationCatalog(engine, AgentExecutorRegistry())


@dataclass
class ScriptedDiscoverer:
    result: ProviderModelDiscovery | ProviderModelInspectionUnavailable
    calls: int = 0

    def discover_models(
        self, configuration: object, auth_profile: object
    ) -> ProviderModelDiscovery | ProviderModelInspectionUnavailable:
        self.calls += 1
        return self.result


def _seeded_configuration(engine: Engine, provider: str, model: str) -> str:
    """Insert a catalog row the way the store durably shapes it.

    `publish_model_registry` only reads the catalog through
    `AgentConfigurationCatalog.agent_configuration_revision`, which does not
    touch the executor registry; seeding through direct rows -- the same
    pattern `tests/integration/test_host_configuration.py` uses -- avoids
    standing up a registered executor just to publish a row.
    """

    profile = AuthProfileRevision(
        f"profile/{provider}/{model}", 1, ProviderId(provider), AuthMode.API_KEY
    )
    configuration = AgentConfigurationRevision(
        model,
        profile.revision_hash,
        AgentExecutorRevision(f"executor/{provider}"),
        AgentExecutionCapability.HEADLESS,
        AgentConfigurationRevisionFormatVersion.V2,
    )
    with engine.begin() as connection:
        connection.execute(
            auth_profile_revisions.insert().values(
                revision_hash=profile.revision_hash.value,
                profile_id=profile.profile_id,
                revision_number=profile.revision_number,
                provider_id=profile.provider_id.value,
                auth_mode=profile.auth_mode.value,
            )
        )
        connection.execute(
            agent_configuration_revisions.insert().values(
                revision_hash=configuration.revision_hash.value,
                model=configuration.model,
                auth_profile_revision_hash=profile.revision_hash.value,
                executor_revision=configuration.executor_revision.value,
                revision_format_version=configuration.revision_format_version,
                requested_capability=configuration.requested_capability.value,
            )
        )
    return configuration.revision_hash.value


def test_publish_model_registry_discovers_and_publishes_a_new_entry(
    engine: Engine,
    channel: DbosHostConfigurationChannel,
    catalog: DbosAgentConfigurationCatalog,
) -> None:
    configuration_hash = _seeded_configuration(engine, "openai", "gpt-5.6")
    discoverer = ScriptedDiscoverer(ProviderModelDiscovery(frozenset({"gpt-5.6"})))

    result = publish_model_registry(
        "openai", 1, (("gpt-5.6", configuration_hash),), channel, catalog, discoverer
    )

    assert isinstance(result, ModelRegistryPublished)
    (entry,) = result.revision.entries
    assert entry.source is ModelRegistryEntrySource.DISCOVERED
    assert entry.provider_check is ProviderModelCheck.CHECKED


def test_publish_model_registry_without_a_discoverer_marks_entries_not_checked(
    engine: Engine,
    channel: DbosHostConfigurationChannel,
    catalog: DbosAgentConfigurationCatalog,
) -> None:
    configuration_hash = _seeded_configuration(engine, "openai", "gpt-5.6")

    result = publish_model_registry(
        "openai", 1, (("gpt-5.6", configuration_hash),), channel, catalog, None
    )

    assert isinstance(result, ModelRegistryPublished)
    (entry,) = result.revision.entries
    assert entry.source is ModelRegistryEntrySource.OPERATOR
    assert entry.provider_check is ProviderModelCheck.NOT_CHECKED


def test_publish_model_registry_marks_a_model_the_provider_does_not_list_as_unknown(
    engine: Engine,
    channel: DbosHostConfigurationChannel,
    catalog: DbosAgentConfigurationCatalog,
) -> None:
    configuration_hash = _seeded_configuration(engine, "openai", "gpt-5.6")
    discoverer = ScriptedDiscoverer(ProviderModelDiscovery(frozenset({"gpt-4"})))

    result = publish_model_registry(
        "openai", 1, (("gpt-5.6", configuration_hash),), channel, catalog, discoverer
    )

    assert isinstance(result, ModelRegistryPublished)
    (entry,) = result.revision.entries
    assert entry.source is ModelRegistryEntrySource.OPERATOR
    assert entry.provider_check is ProviderModelCheck.UNKNOWN_AT_PROVIDER


def test_publish_model_registry_is_unchanged_for_the_same_revision_and_entries(
    engine: Engine,
    channel: DbosHostConfigurationChannel,
    catalog: DbosAgentConfigurationCatalog,
) -> None:
    configuration_hash = _seeded_configuration(engine, "openai", "gpt-5.6")
    entries = (("gpt-5.6", configuration_hash),)
    publish_model_registry("openai", 1, entries, channel, catalog, None)

    result = publish_model_registry("openai", 1, entries, channel, catalog, None)

    assert isinstance(result, ModelRegistryUnchanged)


def test_publish_model_registry_carries_forward_existing_entries_without_rediscovering(
    engine: Engine,
    channel: DbosHostConfigurationChannel,
    catalog: DbosAgentConfigurationCatalog,
) -> None:
    first_hash = _seeded_configuration(engine, "openai", "gpt-5.6")
    discoverer = ScriptedDiscoverer(ProviderModelDiscovery(frozenset({"gpt-5.6"})))
    publish_model_registry(
        "openai", 1, (("gpt-5.6", first_hash),), channel, catalog, discoverer
    )
    assert discoverer.calls == 1
    second_hash = _seeded_configuration(engine, "openai", "gpt-6")

    result = publish_model_registry(
        "openai",
        2,
        (("gpt-5.6", first_hash), ("gpt-6", second_hash)),
        channel,
        catalog,
        discoverer,
    )

    assert isinstance(result, ModelRegistryPublished)
    assert discoverer.calls == 2
    carried, discovered = result.revision.entries
    assert carried.model_id == "gpt-5.6"
    assert carried.source is ModelRegistryEntrySource.DISCOVERED
    assert discovered.model_id == "gpt-6"


def test_publish_model_registry_refuses_an_unknown_configuration_hash(
    channel: DbosHostConfigurationChannel,
    catalog: DbosAgentConfigurationCatalog,
) -> None:
    result = publish_model_registry(
        "openai", 1, (("gpt-5.6", "a" * 64),), channel, catalog, None
    )

    assert isinstance(result, ModelRegistryInvalid)


def test_publish_model_registry_refuses_a_configuration_for_a_different_provider(
    engine: Engine,
    channel: DbosHostConfigurationChannel,
    catalog: DbosAgentConfigurationCatalog,
) -> None:
    configuration_hash = _seeded_configuration(engine, "openai", "gpt-5.6")

    result = publish_model_registry(
        "anthropic", 1, (("gpt-5.6", configuration_hash),), channel, catalog, None
    )

    assert isinstance(result, ModelRegistryInvalid)


def test_publish_model_registry_surfaces_discovery_unavailable_as_write_unavailable(
    engine: Engine,
    channel: DbosHostConfigurationChannel,
    catalog: DbosAgentConfigurationCatalog,
) -> None:
    configuration_hash = _seeded_configuration(engine, "openai", "gpt-5.6")
    discoverer = ScriptedDiscoverer(ProviderModelInspectionUnavailable("provider down"))

    result = publish_model_registry(
        "openai", 1, (("gpt-5.6", configuration_hash),), channel, catalog, discoverer
    )

    assert result == WriteUnavailable("provider down")
