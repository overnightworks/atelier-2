from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from atelier2.contracts.agents import (
    AgentConfigurationRevision,
    AuthProfileRevision,
    ProviderId,
)
from atelier2.contracts.host_configuration import (
    HostModelConfigurationSnapshot,
    ModelRegistryRevision,
    ProjectId,
    ProjectModelDefaultsRevision,
    ProjectRootRevision,
    ProjectSourceConnectionRevision,
    ProjectSourceId,
    ProviderModelCheck,
)
from atelier2.ports.durable_runs import DurableStateCorrupt, DurableWriteUnavailable


@dataclass(frozen=True)
class HostConfigurationReadUnavailable:
    detail: str | None = None


@dataclass(frozen=True)
class ModelRegistryRevisionCreated:
    revision: ModelRegistryRevision


@dataclass(frozen=True)
class ModelRegistryRevisionExisting:
    revision: ModelRegistryRevision


@dataclass(frozen=True)
class ModelRegistryRevisionConflict:
    pass


@dataclass(frozen=True)
class ModelRegistryRevisionCollision:
    pass


@dataclass(frozen=True)
class ProjectModelDefaultsRevisionCreated:
    revision: ProjectModelDefaultsRevision


@dataclass(frozen=True)
class ProjectModelDefaultsRevisionExisting:
    revision: ProjectModelDefaultsRevision


@dataclass(frozen=True)
class ProjectModelDefaultsRevisionConflict:
    pass


@dataclass(frozen=True)
class ProjectModelDefaultsRevisionCollision:
    pass


@dataclass(frozen=True)
class ProjectModelDefaultsRevisionInvalid:
    """A new default is not checked now and is not a carried saved row."""


@dataclass(frozen=True)
class ProviderModelDiscovery:
    model_ids: frozenset[str]


@dataclass(frozen=True)
class ProviderModelDiscoveryUnsupported:
    """This provider exposes no model-list operation, so first use must check."""


@dataclass(frozen=True)
class ProviderModelInspectionUnavailable:
    detail: str | None = None


type ProviderModelDiscoveryResult = (
    ProviderModelDiscovery
    | ProviderModelDiscoveryUnsupported
    | ProviderModelInspectionUnavailable
)
type ProviderModelValidationResult = (
    ProviderModelCheck | ProviderModelInspectionUnavailable
)


class ProviderModelDiscoverer(Protocol):
    """Which models this provider names, as its own narrow protocol.

    A caller composing only discovery depends on this answer alone,
    so a fake or adapter serving the dry-run does not have to grow with it.
    """

    def discover_models(
        self,
        configuration: AgentConfigurationRevision,
        auth_profile: AuthProfileRevision,
    ) -> ProviderModelDiscoveryResult: ...


class ProviderModelValidator(Protocol):
    """Whether this model runs on this executor path, as its own narrow protocol.

    A caller composing only the dry-run depends on this answer alone,
    so a fake or adapter serving discovery does not have to grow with it.
    """

    def validate_model(
        self,
        configuration: AgentConfigurationRevision,
        auth_profile: AuthProfileRevision,
    ) -> ProviderModelValidationResult: ...


type LatestProjectRootResult = (
    ProjectRootRevision | None | HostConfigurationReadUnavailable | DurableStateCorrupt
)

type LatestModelRegistryResult = (
    ModelRegistryRevision
    | None
    | HostConfigurationReadUnavailable
    | DurableStateCorrupt
)

type LatestModelRegistriesResult = (
    tuple[ModelRegistryRevision, ...]
    | HostConfigurationReadUnavailable
    | DurableStateCorrupt
)

type PublishModelRegistryResult = (
    ModelRegistryRevisionCreated
    | ModelRegistryRevisionExisting
    | ModelRegistryRevisionConflict
    | ModelRegistryRevisionCollision
    | DurableWriteUnavailable
    | DurableStateCorrupt
)

type LatestProjectModelDefaultsResult = (
    ProjectModelDefaultsRevision
    | None
    | HostConfigurationReadUnavailable
    | DurableStateCorrupt
)

type HostModelConfigurationSnapshotResult = (
    HostModelConfigurationSnapshot
    | HostConfigurationReadUnavailable
    | DurableStateCorrupt
)

type PublishProjectModelDefaultsResult = (
    ProjectModelDefaultsRevisionCreated
    | ProjectModelDefaultsRevisionExisting
    | ProjectModelDefaultsRevisionConflict
    | ProjectModelDefaultsRevisionCollision
    | ProjectModelDefaultsRevisionInvalid
    | DurableWriteUnavailable
    | DurableStateCorrupt
)


@dataclass(frozen=True)
class ProjectSourceConnectionRevisionCreated:
    revision: ProjectSourceConnectionRevision


@dataclass(frozen=True)
class ProjectSourceConnectionRevisionExisting:
    revision: ProjectSourceConnectionRevision


@dataclass(frozen=True)
class ProjectSourceConnectionRevisionConflict:
    pass


@dataclass(frozen=True)
class ProjectSourceConnectionRevisionCollision:
    pass


@dataclass(frozen=True)
class ProjectSourceCredentialDirectoryReferenced:
    pass


@dataclass(frozen=True)
class ProjectSourceCredentialDirectoryUnreferenced:
    pass


type LatestProjectSourceConnectionResult = (
    ProjectSourceConnectionRevision
    | None
    | HostConfigurationReadUnavailable
    | DurableStateCorrupt
)

type LatestProjectSourceConnectionsResult = (
    tuple[ProjectSourceConnectionRevision, ...]
    | HostConfigurationReadUnavailable
    | DurableStateCorrupt
)

type ProjectSourceCredentialDirectoryReferenceResult = (
    ProjectSourceCredentialDirectoryReferenced
    | ProjectSourceCredentialDirectoryUnreferenced
    | HostConfigurationReadUnavailable
    | DurableStateCorrupt
)

type PublishProjectSourceConnectionResult = (
    ProjectSourceConnectionRevisionCreated
    | ProjectSourceConnectionRevisionExisting
    | ProjectSourceConnectionRevisionConflict
    | ProjectSourceConnectionRevisionCollision
    | DurableWriteUnavailable
    | DurableStateCorrupt
)


class HostConfigurationChannel(Protocol):
    def latest_project_root_revision(
        self, project_id: ProjectId
    ) -> LatestProjectRootResult: ...

    def latest_model_registry_revision(
        self, provider_id: ProviderId
    ) -> LatestModelRegistryResult: ...

    def latest_model_registry_revisions(self) -> LatestModelRegistriesResult: ...

    def publish_model_registry_revision(
        self, revision: ModelRegistryRevision
    ) -> PublishModelRegistryResult: ...

    def latest_project_model_defaults_revision(
        self, project_id: ProjectId
    ) -> LatestProjectModelDefaultsResult: ...

    def model_configuration_snapshot(
        self, project_id: ProjectId | None
    ) -> HostModelConfigurationSnapshotResult: ...

    def publish_project_model_defaults_revision(
        self, revision: ProjectModelDefaultsRevision
    ) -> PublishProjectModelDefaultsResult:
        """Validate exact registry references and append in one transaction."""

        ...


class ProjectSourceConnectionChannel(Protocol):
    """The channel's third family, as its own narrow protocol.

    A caller composing only the connection record depends on this family alone,
    so a fake or adapter serving another family does not have to grow with it.
    """

    def latest_project_source_connection_revision(
        self, project_id: ProjectId
    ) -> LatestProjectSourceConnectionResult: ...

    def latest_project_source_connection_revisions(
        self, project_id: ProjectId
    ) -> LatestProjectSourceConnectionsResult: ...

    def latest_project_source_connection_revision_by_source(
        self, project_id: ProjectId, source_id: ProjectSourceId
    ) -> LatestProjectSourceConnectionResult: ...

    def project_source_credential_directory_reference(
        self, project_id: ProjectId, credential_directory: Path
    ) -> ProjectSourceCredentialDirectoryReferenceResult: ...

    def publish_project_source_connection_revision(
        self, revision: ProjectSourceConnectionRevision
    ) -> PublishProjectSourceConnectionResult: ...
