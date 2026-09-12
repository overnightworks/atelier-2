"""Publish and read provider registries, project defaults, and role resolutions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import assert_never, cast

from atelier2.application.read_projects import (
    ProjectRead,
    ServedProjectUnknown,
    get_project,
)
from atelier2.application.refusals import (
    DurableStateCorrupt,
    ReadUnavailable,
    WriteUnavailable,
)
from atelier2.application.resolve_start_bindings import (
    CastUnboundRolesResult,
    cast_unbound_roles,
    undeclared_agent_role_refusal,
)
from atelier2.application.role_candidates import registered_configurations
from atelier2.contracts.agents import (
    AgentBinding,
    AgentBindingSet,
    AgentConfigurationRevision,
    AgentConfigurationRevisionHash,
    AgentRole,
    AuthProfileRevision,
    AuthProfileRevisionHash,
    ProviderId,
)
from atelier2.contracts.host_configuration import (
    HostModelConfigurationSnapshot,
    HostModelRegistryRevisionHash,
    ModelRegistryEntry,
    ModelRegistryEntrySource,
    ModelRegistryRevision,
    ProjectId,
    ProjectModelDefault,
    ProjectModelDefaultsRevision,
    ProjectUnknown,
    ProviderModelCheck,
)
from atelier2.contracts.runs import WorkflowRevisionHash
from atelier2.contracts.workflows_v3 import RoleDifficulty, WorkflowGraphV3
from atelier2.ports.agent_configurations import AgentConfigurationCatalog
from atelier2.ports.durable_runs import DurableStateCorrupt as PortDurableStateCorrupt
from atelier2.ports.durable_runs import (
    DurableWriteUnavailable,
)
from atelier2.ports.host_configuration import (
    HostConfigurationChannel,
    HostConfigurationReadUnavailable,
    ModelRegistryRevisionCreated,
    ModelRegistryRevisionExisting,
    ProjectModelDefaultsRevisionCreated,
    ProjectModelDefaultsRevisionExisting,
    ProjectModelDefaultsRevisionInvalid,
    ProviderModelDiscoverer,
    ProviderModelDiscovery,
    ProviderModelDiscoveryResult,
    ProviderModelDiscoveryUnsupported,
    ProviderModelInspectionUnavailable,
    ProviderModelValidator,
)
from atelier2.ports.host_configuration import (
    ModelRegistryRevisionCollision as PortModelRegistryRevisionCollision,
)
from atelier2.ports.host_configuration import (
    ModelRegistryRevisionConflict as PortModelRegistryRevisionConflict,
)
from atelier2.ports.host_configuration import (
    ProjectModelDefaultsRevisionCollision as PortProjectModelDefaultsRevisionCollision,
)
from atelier2.ports.host_configuration import (
    ProjectModelDefaultsRevisionConflict as PortProjectModelDefaultsRevisionConflict,
)
from atelier2.ports.workflow_revisions import (
    ProjectionTooLarge,
    QueryDurableStateCorrupt,
    WorkflowRevisionFound,
    WorkflowRevisionMissing,
    WorkflowRevisionQueries,
)
from atelier2.ports.workflow_revisions import (
    ReadUnavailable as WorkflowReadUnavailable,
)


@dataclass(frozen=True)
class ModelRegistryRead:
    revision: ModelRegistryRevision


@dataclass(frozen=True)
class ModelRegistryMissing:
    pass


@dataclass(frozen=True)
class ModelRegistryPublished:
    revision: ModelRegistryRevision


@dataclass(frozen=True)
class ModelRegistryUnchanged:
    revision: ModelRegistryRevision


@dataclass(frozen=True)
class ModelRegistryConflict:
    pass


@dataclass(frozen=True)
class ModelRegistryCollision:
    pass


@dataclass(frozen=True)
class ModelRegistryInvalid:
    pass


type GetModelRegistryResult = (
    ModelRegistryRead
    | ModelRegistryMissing
    | ModelRegistryInvalid
    | ReadUnavailable
    | DurableStateCorrupt
)
type PublishModelRegistryUseCaseResult = (
    ModelRegistryPublished
    | ModelRegistryUnchanged
    | ModelRegistryConflict
    | ModelRegistryCollision
    | ModelRegistryInvalid
    | WriteUnavailable
    | DurableStateCorrupt
)


@dataclass(frozen=True)
class ProjectModelDefaultsRead:
    revision: ProjectModelDefaultsRevision


@dataclass(frozen=True)
class ProjectModelDefaultsMissing:
    pass


@dataclass(frozen=True)
class ProjectModelDefaultsPublished:
    revision: ProjectModelDefaultsRevision


@dataclass(frozen=True)
class ProjectModelDefaultsUnchanged:
    revision: ProjectModelDefaultsRevision


@dataclass(frozen=True)
class ProjectModelDefaultsConflict:
    pass


@dataclass(frozen=True)
class ProjectModelDefaultsCollision:
    pass


@dataclass(frozen=True)
class ProjectModelDefaultsInvalid:
    pass


@dataclass(frozen=True)
class ModelConfigurationProjectUnknown:
    pass


type GetProjectModelDefaultsResult = (
    ProjectModelDefaultsRead
    | ProjectModelDefaultsMissing
    | ModelConfigurationProjectUnknown
    | ReadUnavailable
    | DurableStateCorrupt
)
type PublishProjectModelDefaultsUseCaseResult = (
    ProjectModelDefaultsPublished
    | ProjectModelDefaultsUnchanged
    | ProjectModelDefaultsConflict
    | ProjectModelDefaultsCollision
    | ProjectModelDefaultsInvalid
    | ModelConfigurationProjectUnknown
    | WriteUnavailable
    | DurableStateCorrupt
)


@dataclass(frozen=True)
class ProjectModelResolutionRead:
    resolution: CastUnboundRolesResult


@dataclass(frozen=True)
class ModelResolutionWorkflowMissing:
    pass


@dataclass(frozen=True)
class ModelResolutionWorkflowUnsupported:
    pass


@dataclass(frozen=True)
class ModelResolutionInvalidAgentBindings:
    """The resolution request names a role this workflow did not declare."""


type GetProjectModelResolutionResult = (
    ProjectModelResolutionRead
    | ModelConfigurationProjectUnknown
    | ModelResolutionWorkflowMissing
    | ModelResolutionWorkflowUnsupported
    | ModelResolutionInvalidAgentBindings
    | ModelRegistryInvalid
    | ReadUnavailable
    | DurableStateCorrupt
)


def get_model_registry(
    provider_id: str, channel: HostConfigurationChannel
) -> GetModelRegistryResult:
    try:
        provider = ProviderId(provider_id)
    except (TypeError, ValueError):
        return ModelRegistryInvalid()
    match channel.latest_model_registry_revision(provider):
        case ModelRegistryRevision() as revision:
            return ModelRegistryRead(revision)
        case None:
            return ModelRegistryMissing()
        case HostConfigurationReadUnavailable(detail):
            return ReadUnavailable(detail)
        case PortDurableStateCorrupt():
            return DurableStateCorrupt()
        case _ as unreachable:
            assert_never(unreachable)


@dataclass(frozen=True)
class _CarriedModelRegistryEntries:
    """The registry entries a new revision may still reuse, keyed the same
    way the requested entries are, because their revision either did not
    change or changed to exactly this set."""

    by_model_and_configuration: dict[
        tuple[str, AgentConfigurationRevisionHash], ModelRegistryEntry
    ]


def _carried_model_registry_entries(
    provider: ProviderId,
    revision_number: int,
    requested: tuple[tuple[str, AgentConfigurationRevisionHash], ...],
    channel: HostConfigurationChannel,
) -> (
    _CarriedModelRegistryEntries
    | ModelRegistryUnchanged
    | WriteUnavailable
    | DurableStateCorrupt
):
    match channel.latest_model_registry_revision(provider):
        case ModelRegistryRevision() as latest:
            latest_entries = {
                (entry.model_id, entry.agent_configuration_revision_hash): entry
                for entry in latest.entries
            }
            if (
                latest.revision_number == revision_number
                and len(latest_entries) == len(requested)
                and frozenset(latest_entries) == frozenset(requested)
            ):
                return ModelRegistryUnchanged(latest)
            return _CarriedModelRegistryEntries(latest_entries)
        case None:
            return _CarriedModelRegistryEntries({})
        case HostConfigurationReadUnavailable(detail):
            return WriteUnavailable(detail)
        case PortDurableStateCorrupt():
            return DurableStateCorrupt()
        case _ as unreachable:
            assert_never(unreachable)


def _discovered_model(
    configuration: AgentConfigurationRevision,
    auth_profile: AuthProfileRevision,
    discoverer: ProviderModelDiscoverer | None,
    discovery_by_auth_profile: dict[
        AuthProfileRevisionHash, ProviderModelDiscoveryResult
    ],
) -> ProviderModelDiscoveryResult:
    """The one discovery answer this auth profile already asked for, or a
    fresh answer -- cached so a provider a role shares many models with is
    asked once, not once per requested model."""

    discovery = discovery_by_auth_profile.get(auth_profile.revision_hash)
    if discovery is None:
        discovery = (
            ProviderModelDiscoveryUnsupported()
            if discoverer is None
            else discoverer.discover_models(configuration, auth_profile)
        )
        discovery_by_auth_profile[auth_profile.revision_hash] = discovery
    return discovery


def _model_registry_entry_provenance(
    model_id: str, model_ids: frozenset[str]
) -> tuple[ModelRegistryEntrySource, ProviderModelCheck]:
    if model_id in model_ids:
        return ModelRegistryEntrySource.DISCOVERED, ProviderModelCheck.CHECKED
    return ModelRegistryEntrySource.OPERATOR, ProviderModelCheck.UNKNOWN_AT_PROVIDER


def _inspected_model_registry_entry(
    provider: ProviderId,
    model_id: str,
    configuration_hash: AgentConfigurationRevisionHash,
    carried: _CarriedModelRegistryEntries,
    discovery_by_auth_profile: dict[
        AuthProfileRevisionHash, ProviderModelDiscoveryResult
    ],
    catalog: AgentConfigurationCatalog,
    discoverer: ProviderModelDiscoverer | None,
) -> ModelRegistryEntry | ModelRegistryInvalid | WriteUnavailable:
    """What one requested model_id/configuration pair contributes to the
    published revision: the carried entry it already had, or a freshly
    inspected one."""

    found = catalog.agent_configuration_revision(configuration_hash)
    if found is None:
        return ModelRegistryInvalid()
    configuration, auth_profile = found
    if configuration.model != model_id or auth_profile.provider_id != provider:
        return ModelRegistryInvalid()
    entry = carried.by_model_and_configuration.get((model_id, configuration_hash))
    if entry is not None:
        return entry
    discovery = _discovered_model(
        configuration, auth_profile, discoverer, discovery_by_auth_profile
    )
    match discovery:
        case ProviderModelDiscovery(model_ids):
            source, provider_check = _model_registry_entry_provenance(
                model_id, model_ids
            )
        case ProviderModelDiscoveryUnsupported():
            source = ModelRegistryEntrySource.OPERATOR
            provider_check = ProviderModelCheck.NOT_CHECKED
        case ProviderModelInspectionUnavailable(detail):
            return WriteUnavailable(detail)
        case _ as unreachable:
            assert_never(unreachable)
    return ModelRegistryEntry(model_id, configuration_hash, source, provider_check)


def _stored_model_registry_revision(
    channel: HostConfigurationChannel, revision: ModelRegistryRevision
) -> PublishModelRegistryUseCaseResult:
    match channel.publish_model_registry_revision(revision):
        case ModelRegistryRevisionCreated(stored):
            return ModelRegistryPublished(stored)
        case ModelRegistryRevisionExisting(stored):
            return ModelRegistryUnchanged(stored)
        case PortModelRegistryRevisionConflict():
            return ModelRegistryConflict()
        case PortModelRegistryRevisionCollision():
            return ModelRegistryCollision()
        case DurableWriteUnavailable():
            return WriteUnavailable()
        case PortDurableStateCorrupt():
            return DurableStateCorrupt()
        case _ as unreachable:
            assert_never(unreachable)


def publish_model_registry(
    provider_id: str,
    revision_number: int,
    entries: tuple[tuple[str, str], ...],
    channel: HostConfigurationChannel,
    catalog: AgentConfigurationCatalog,
    discoverer: ProviderModelDiscoverer | None,
) -> PublishModelRegistryUseCaseResult:
    try:
        provider = ProviderId(provider_id)
        requested = tuple(
            (model_id, AgentConfigurationRevisionHash(configuration_hash))
            for model_id, configuration_hash in entries
        )
    except (TypeError, ValueError):
        return ModelRegistryInvalid()
    carried = _carried_model_registry_entries(
        provider, revision_number, requested, channel
    )
    match carried:
        case ModelRegistryUnchanged() | WriteUnavailable() | DurableStateCorrupt():
            return carried
        case _CarriedModelRegistryEntries():
            pass
        case _ as unreachable:
            assert_never(unreachable)
    inspected: list[ModelRegistryEntry] = []
    discovery_by_auth_profile: dict[
        AuthProfileRevisionHash, ProviderModelDiscoveryResult
    ] = {}
    for model_id, configuration_hash in requested:
        entry_result = _inspected_model_registry_entry(
            provider,
            model_id,
            configuration_hash,
            carried,
            discovery_by_auth_profile,
            catalog,
            discoverer,
        )
        match entry_result:
            case ModelRegistryEntry():
                inspected.append(entry_result)
            case ModelRegistryInvalid() | WriteUnavailable():
                return entry_result
            case _ as unreachable:
                assert_never(unreachable)
    try:
        revision = ModelRegistryRevision(provider, revision_number, tuple(inspected))
    except (TypeError, ValueError):
        return ModelRegistryInvalid()
    return _stored_model_registry_revision(channel, revision)


def validate_model_registry_entry(
    provider_id: str,
    configuration_hash: str,
    channel: HostConfigurationChannel,
    catalog: AgentConfigurationCatalog,
    validator: ProviderModelValidator | None,
) -> PublishModelRegistryUseCaseResult:
    try:
        provider = ProviderId(provider_id)
        configuration_revision_hash = AgentConfigurationRevisionHash(configuration_hash)
    except (TypeError, ValueError):
        return ModelRegistryInvalid()
    match channel.latest_model_registry_revision(provider):
        case ModelRegistryRevision() as latest:
            pass
        case None:
            return ModelRegistryInvalid()
        case HostConfigurationReadUnavailable(detail):
            return WriteUnavailable(detail)
        case PortDurableStateCorrupt():
            return DurableStateCorrupt()
        case _ as unreachable:
            assert_never(unreachable)
    selected = next(
        (
            entry
            for entry in latest.entries
            if entry.agent_configuration_revision_hash == configuration_revision_hash
        ),
        None,
    )
    if selected is None:
        return ModelRegistryInvalid()
    if selected.provider_check is not ProviderModelCheck.NOT_CHECKED:
        return ModelRegistryUnchanged(latest)
    found = catalog.agent_configuration_revision(configuration_revision_hash)
    if found is None or validator is None:
        return ModelRegistryInvalid()
    configuration, auth_profile = found
    if configuration.model != selected.model_id or auth_profile.provider_id != provider:
        return ModelRegistryInvalid()
    match validator.validate_model(configuration, auth_profile):
        case ProviderModelCheck.CHECKED as check:
            pass
        case ProviderModelCheck.UNKNOWN_AT_PROVIDER as check:
            pass
        case ProviderModelCheck.NOT_CHECKED:
            return ModelRegistryInvalid()
        case ProviderModelInspectionUnavailable(detail):
            return WriteUnavailable(detail)
        case _ as unreachable:
            assert_never(unreachable)
    try:
        revised = ModelRegistryRevision(
            provider,
            latest.revision_number + 1,
            tuple(
                ModelRegistryEntry(
                    entry.model_id,
                    entry.agent_configuration_revision_hash,
                    entry.source,
                    check
                    if entry.agent_configuration_revision_hash
                    == configuration_revision_hash
                    else entry.provider_check,
                )
                for entry in latest.entries
            ),
        )
    except (TypeError, ValueError):
        return ModelRegistryInvalid()
    return _stored_model_registry_revision(channel, revised)


def _project(project_id: str) -> ProjectId | None:
    try:
        return ProjectId(project_id)
    except (ProjectUnknown, TypeError, ValueError):
        return None


def get_project_model_defaults(
    project_id: str,
    served_project_id: ProjectId | None,
    channel: HostConfigurationChannel,
) -> GetProjectModelDefaultsResult:
    project = _project(project_id)
    if project is None:
        return ModelConfigurationProjectUnknown()
    match get_project(project, served_project_id, channel):
        case ProjectRead():
            pass
        case ServedProjectUnknown():
            return ModelConfigurationProjectUnknown()
        case ReadUnavailable() as unavailable:
            return unavailable
        case DurableStateCorrupt() as corrupt:
            return corrupt
        case _ as unreachable:
            assert_never(unreachable)
    match channel.latest_project_model_defaults_revision(project):
        case ProjectModelDefaultsRevision() as revision:
            return ProjectModelDefaultsRead(revision)
        case None:
            return ProjectModelDefaultsMissing()
        case HostConfigurationReadUnavailable(detail):
            return ReadUnavailable(detail)
        case PortDurableStateCorrupt():
            return DurableStateCorrupt()
        case _ as unreachable:
            assert_never(unreachable)


def publish_project_model_defaults(
    project_id: str,
    served_project_id: ProjectId | None,
    revision_number: int,
    defaults: tuple[tuple[int, str, str, str, str], ...],
    channel: HostConfigurationChannel,
) -> PublishProjectModelDefaultsUseCaseResult:
    project = _project(project_id)
    if project is None:
        return ModelConfigurationProjectUnknown()
    match get_project(project, served_project_id, channel):
        case ProjectRead():
            pass
        case ServedProjectUnknown():
            return ModelConfigurationProjectUnknown()
        case ReadUnavailable(detail):
            return WriteUnavailable(detail)
        case DurableStateCorrupt() as corrupt:
            return corrupt
        case _ as unreachable:
            assert_never(unreachable)
    try:
        revision = ProjectModelDefaultsRevision(
            project,
            revision_number,
            tuple(
                ProjectModelDefault(
                    cast(RoleDifficulty, difficulty),
                    HostModelRegistryRevisionHash(registry_hash),
                    ProviderId(provider_id),
                    model_id,
                    AgentConfigurationRevisionHash(configuration_hash),
                )
                for (
                    difficulty,
                    registry_hash,
                    provider_id,
                    model_id,
                    configuration_hash,
                ) in defaults
            ),
        )
    except (TypeError, ValueError):
        return ProjectModelDefaultsInvalid()
    match channel.publish_project_model_defaults_revision(revision):
        case ProjectModelDefaultsRevisionCreated(stored):
            return ProjectModelDefaultsPublished(stored)
        case ProjectModelDefaultsRevisionExisting(stored):
            return ProjectModelDefaultsUnchanged(stored)
        case PortProjectModelDefaultsRevisionConflict():
            return ProjectModelDefaultsConflict()
        case PortProjectModelDefaultsRevisionCollision():
            return ProjectModelDefaultsCollision()
        case ProjectModelDefaultsRevisionInvalid():
            return ProjectModelDefaultsInvalid()
        case DurableWriteUnavailable():
            return WriteUnavailable()
        case PortDurableStateCorrupt():
            return DurableStateCorrupt()
        case _ as unreachable:
            assert_never(unreachable)


def get_project_model_resolution(
    project_id: str,
    served_project_id: ProjectId | None,
    workflow_revision_hash: str,
    requested: tuple[tuple[str, str], ...],
    channel: HostConfigurationChannel,
    workflow_queries: WorkflowRevisionQueries,
    catalog: AgentConfigurationCatalog,
) -> GetProjectModelResolutionResult:
    project = _project(project_id)
    if project is None:
        return ModelConfigurationProjectUnknown()
    match get_project(project, served_project_id, channel):
        case ProjectRead():
            pass
        case ServedProjectUnknown():
            return ModelConfigurationProjectUnknown()
        case ReadUnavailable() as unavailable:
            return unavailable
        case DurableStateCorrupt() as corrupt:
            return corrupt
        case _ as unreachable:
            assert_never(unreachable)
    try:
        revision_hash = WorkflowRevisionHash(workflow_revision_hash)
        requested_bindings = AgentBindingSet(
            tuple(
                AgentBinding(
                    AgentRole(role), AgentConfigurationRevisionHash(configuration_hash)
                )
                for role, configuration_hash in requested
            )
        )
    except (TypeError, ValueError):
        return ModelRegistryInvalid()
    match workflow_queries.get_workflow_revision(revision_hash):
        case WorkflowRevisionFound(projection):
            if not isinstance(projection.graph, WorkflowGraphV3):
                return ModelResolutionWorkflowUnsupported()
            graph = projection.graph
        case WorkflowRevisionMissing():
            return ModelResolutionWorkflowMissing()
        case WorkflowReadUnavailable(detail):
            return ReadUnavailable(detail)
        case ProjectionTooLarge() | QueryDurableStateCorrupt():
            return DurableStateCorrupt()
        case _ as unreachable:
            assert_never(unreachable)
    if undeclared_agent_role_refusal(graph, requested_bindings) is not None:
        return ModelResolutionInvalidAgentBindings()
    match channel.model_configuration_snapshot(project):
        case HostModelConfigurationSnapshot(registries, defaults):
            return ProjectModelResolutionRead(
                cast_unbound_roles(
                    graph,
                    requested_bindings,
                    defaults,
                    registries,
                    registered_configurations(registries, requested_bindings, catalog),
                )
            )
        case HostConfigurationReadUnavailable(detail):
            return ReadUnavailable(detail)
        case PortDurableStateCorrupt():
            return DurableStateCorrupt()
        case _ as unreachable:
            assert_never(unreachable)
