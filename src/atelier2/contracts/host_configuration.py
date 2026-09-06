"""The host's one live-versioned configuration channel.

The channel owns project roots, provider model registries, project model
defaults, and project source connections. Model entries point at immutable
agent configurations: that existing configuration carries today's public
auth-profile predecessor of ADR 0017's Account, while no credential value ever
enters this channel. CLI flags name where the channel lives; they are not a
second copy of it.
"""

from __future__ import annotations

import re
import struct
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from atelier2.contracts.agents import (
    MAXIMUM_AGENT_FIELD_CHARACTERS,
    MAXIMUM_SIGNED_INT64,
    AgentConfigurationRevisionHash,
    ProviderId,
)
from atelier2.contracts.hashing import Sha256Hash, frame
from atelier2.contracts.when import RecordedAt
from atelier2.contracts.workflows_v3 import RoleDifficulty

MAXIMUM_PROJECT_ID_CHARACTERS = 1_024
# This serve slice opens no more than the single project id bound at composition.
# The collection, application decision, and OpenAPI all answer to this owner.
MAXIMUM_SERVED_PROJECTS = 1
# Linux PATH_MAX is 4096 including the terminating NUL; a 4096-character
# path is not openable. The published CHECK must match what open will admit.
MAXIMUM_PROJECT_ROOT_PATH_CHARACTERS = 4_095
# A provider's discovered or operator-added catalog is configuration, not a
# product inventory. The bound prevents one immutable revision becoming an
# unbounded document without constraining which exact ids may appear in it.
MAXIMUM_MODEL_REGISTRY_ENTRIES = 100
MAXIMUM_PROJECT_MODEL_DEFAULTS = 3
MAXIMUM_EXACT_MODEL_ID_CHARACTERS = MAXIMUM_AGENT_FIELD_CHARACTERS
EXACT_MODEL_ID_PATTERN = r"^\S+$"
# A source kind names one platform adapter family; it is a short word, not an
# address.
MAXIMUM_SOURCE_KIND_CHARACTERS = 64
# The source address is opaque here, so its bound mirrors the tracker item
# reference's: room for any platform's own identifier, no room for a document.
MAXIMUM_SOURCE_ADDRESS_CHARACTERS = 1_024
MAXIMUM_SOURCE_REFERENCE_CHARACTERS = 1_024
# A managed connection receives one provider token through the bounded HTTP
# door before depositing it outside durable configuration.
MAXIMUM_SOURCE_TOKEN_CHARACTERS = 4_096
# This slice admits at most one active source for a project. The list door and
# application decision share this bound until multi-source identity lands.
MAXIMUM_ACTIVE_PROJECT_SOURCES = 1
MAXIMUM_CONNECTION_ACTOR_CHARACTERS = 1_024
# The credential reference is a filesystem directory, so the project-root
# bound's PATH_MAX rationale is this bound too.
MAXIMUM_CREDENTIAL_DIRECTORY_CHARACTERS = MAXIMUM_PROJECT_ROOT_PATH_CHARACTERS
SOURCE_ID_PATTERN = r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
_SOURCE_ID = re.compile(SOURCE_ID_PATTERN)

PROJECT_UNKNOWN = "project-unknown"
PROJECT_ROOT_MISSING = "project-root-missing"
HOST_CONFIGURATION_UNREADABLE = "host-configuration-unreadable"
MODEL_REGISTRY_REVISION_CONFLICT = "model-registry-revision-conflict"
PROJECT_MODEL_DEFAULTS_REVISION_CONFLICT = "project-model-defaults-revision-conflict"


class ProjectUnknown(Exception):
    """The id is malformed, or names a project with no configured root.

    The second case is ADR 0011's service refusal. The channel's missing row
    remains `ProjectRootMissing`.
    """


class ProjectRootMissing(Exception):
    """The channel has no root-path row for this project id."""


class HostConfigurationUnreadable(Exception):
    """The configuration channel could not be read."""


class ProjectRootRevisionConflict(Exception):
    """The same project id and revision number already hold different bytes."""


class ProjectRootBytesDisagree(Exception):
    """Stored project-root fields do not hash to the revision hash they carry."""


@dataclass(frozen=True)
class ProjectId:
    value: str

    def __post_init__(self) -> None:
        if (
            type(self.value) is not str
            or not 1 <= len(self.value) <= MAXIMUM_PROJECT_ID_CHARACTERS
        ):
            raise ProjectUnknown(
                f"{PROJECT_UNKNOWN}: a project id must contain "
                f"1..{MAXIMUM_PROJECT_ID_CHARACTERS} exact characters"
            )
        try:
            encoded = self.value.encode("utf-8")
        except UnicodeEncodeError as error:
            raise ProjectUnknown(
                f"{PROJECT_UNKNOWN}: a project id must be exact UTF-8 Unicode scalar text"
            ) from error
        if encoded.decode("utf-8") != self.value:
            raise ProjectUnknown(
                f"{PROJECT_UNKNOWN}: a project id must round-trip exact UTF-8"
            )


class HostProjectRootRevisionHash(Sha256Hash):
    """Identity of one immutable project-root mapping revision."""


@dataclass(frozen=True)
class ProjectRootRevision:
    project_id: ProjectId
    revision_number: int
    root_path: Path
    revision_hash: HostProjectRootRevisionHash = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.project_id, ProjectId):
            raise TypeError("project id must use its typed contract")
        if (
            type(self.revision_number) is not int
            or not 1 <= self.revision_number <= MAXIMUM_SIGNED_INT64
        ):
            raise ValueError(
                "host project-root revision number must be a positive signed int64"
            )
        if not isinstance(self.root_path, Path):
            raise TypeError("project root path must be a path")
        stored = str(self.root_path.expanduser().resolve())
        if not 1 <= len(stored) <= MAXIMUM_PROJECT_ROOT_PATH_CHARACTERS:
            raise ValueError(
                "project root path must contain "
                f"1..{MAXIMUM_PROJECT_ROOT_PATH_CHARACTERS} exact characters"
            )
        object.__setattr__(self, "root_path", Path(stored))
        object.__setattr__(
            self,
            "revision_hash",
            HostProjectRootRevisionHash.of(
                frame(
                    "host-project-root-revision/v1",
                    self.project_id.value.encode("utf-8"),
                    struct.pack(">Q", self.revision_number),
                    stored.encode("utf-8"),
                )
            ),
        )


class ModelRegistryRevisionConflict(Exception):
    """The same provider and revision number already hold different bytes."""


class ModelRegistryBytesDisagree(Exception):
    """Stored registry fields do not hash to the revision hash they carry."""


class ModelRegistryRevisionHashCollision(Exception):
    """The same registry revision hash already names different fields."""


class ProjectModelDefaultsRevisionConflict(Exception):
    """The same project and revision number already hold different bytes."""


class ProjectModelDefaultsBytesDisagree(Exception):
    """Stored model-default fields do not hash to their revision hash."""


class ProjectModelDefaultsRevisionHashCollision(Exception):
    """The same defaults revision hash already names different fields."""


class HostModelRegistryRevisionHash(Sha256Hash):
    """Identity of one provider's immutable exact-model registry revision."""


class HostProjectModelDefaultsRevisionHash(Sha256Hash):
    """Identity of one project's immutable three-row model-default revision."""


class ModelRegistryEntrySource(StrEnum):
    """How an exact id entered configuration; never a rating or capability."""

    DISCOVERED = "discovered"
    OPERATOR = "operator"


class ProviderModelCheck(StrEnum):
    """What the provider has said about one exact model id."""

    NOT_CHECKED = "not-checked"
    CHECKED = "checked"
    UNKNOWN_AT_PROVIDER = "unknown-at-provider"


class ModelResolutionUncastReason(StrEnum):
    """Why one declared role has no configuration at the casting boundary."""

    OVERRIDE_NOT_REGISTERED = "override-not-registered"
    WORKFLOW_MODEL_NOT_REGISTERED = "workflow-model-not-registered"
    WORKFLOW_MODEL_AMBIGUOUS = "workflow-model-ambiguous"
    NO_PROJECT_DEFAULT = "no-project-default"
    FAMILY_DIFFERENCE_UNAVAILABLE = "family-difference-unavailable"


@dataclass(frozen=True)
class UncastRole:
    """One role a start cannot cast, including the workflow's family condition."""

    role: str
    reason: ModelResolutionUncastReason
    family_differs_from: str | None

    def __post_init__(self) -> None:
        if type(self.role) is not str or self.role == "":
            raise ValueError("an uncast role must be named")
        if not isinstance(self.reason, ModelResolutionUncastReason):
            raise TypeError("an uncast role reason must use its typed contract")
        if self.family_differs_from is not None and (
            type(self.family_differs_from) is not str or self.family_differs_from == ""
        ):
            raise ValueError("an uncast family reference must name a role")


def _exact_model_id(value: object) -> str:
    if (
        type(value) is not str
        or not 1 <= len(value) <= MAXIMUM_EXACT_MODEL_ID_CHARACTERS
        or any(character.isspace() for character in value)
    ):
        raise ValueError(
            "a model id must contain 1.."
            f"{MAXIMUM_EXACT_MODEL_ID_CHARACTERS} exact non-whitespace characters"
        )
    return value


@dataclass(frozen=True)
class ModelRegistryEntry:
    """One exact provider id and the configured Account/executor that runs it."""

    model_id: str
    agent_configuration_revision_hash: AgentConfigurationRevisionHash
    source: ModelRegistryEntrySource
    provider_check: ProviderModelCheck

    def __post_init__(self) -> None:
        _exact_model_id(self.model_id)
        if not isinstance(
            self.agent_configuration_revision_hash, AgentConfigurationRevisionHash
        ):
            raise TypeError("model registry configuration hash must be typed")
        if not isinstance(self.source, ModelRegistryEntrySource):
            raise TypeError("model registry source must use its typed contract")
        if not isinstance(self.provider_check, ProviderModelCheck):
            raise TypeError("model registry provider check must use its typed contract")


@dataclass(frozen=True)
class ModelRegistryRevision:
    provider_id: ProviderId
    revision_number: int
    entries: tuple[ModelRegistryEntry, ...]
    revision_hash: HostModelRegistryRevisionHash = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.provider_id, ProviderId):
            raise TypeError("model registry provider must use its typed contract")
        if (
            type(self.revision_number) is not int
            or not 1 <= self.revision_number <= MAXIMUM_SIGNED_INT64
        ):
            raise ValueError(
                "model registry revision number must be a positive signed int64"
            )
        if not isinstance(self.entries, tuple) or any(
            not isinstance(entry, ModelRegistryEntry) for entry in self.entries
        ):
            raise TypeError("model registry entries must use their typed contract")
        ordered = tuple(
            sorted(self.entries, key=lambda entry: entry.model_id.encode("utf-8"))
        )
        if len({entry.model_id for entry in ordered}) != len(ordered):
            raise ValueError("model registry model ids must be unique per provider")
        if len(ordered) > MAXIMUM_MODEL_REGISTRY_ENTRIES:
            raise ValueError(
                "model registry revisions must contain at most "
                f"{MAXIMUM_MODEL_REGISTRY_ENTRIES} entries"
            )
        object.__setattr__(self, "entries", ordered)
        object.__setattr__(
            self,
            "revision_hash",
            HostModelRegistryRevisionHash.of(
                frame(
                    "host-model-registry-revision/v1",
                    self.provider_id.value.encode("ascii"),
                    struct.pack(">Q", self.revision_number),
                    *(
                        frame(
                            "host-model-registry-entry/v1",
                            entry.model_id.encode("utf-8"),
                            entry.agent_configuration_revision_hash.value.encode(
                                "ascii"
                            ),
                            entry.source.value.encode("ascii"),
                            entry.provider_check.value.encode("ascii"),
                        )
                        for entry in ordered
                    ),
                )
            ),
        )


@dataclass(frozen=True)
class ProjectModelDefault:
    difficulty: RoleDifficulty
    model_registry_revision_hash: HostModelRegistryRevisionHash
    provider_id: ProviderId
    model_id: str
    agent_configuration_revision_hash: AgentConfigurationRevisionHash

    def __post_init__(self) -> None:
        if type(self.difficulty) is not int or self.difficulty not in {1, 2, 3}:
            raise ValueError("a model default difficulty must be 1, 2 or 3")
        if not isinstance(
            self.model_registry_revision_hash, HostModelRegistryRevisionHash
        ):
            raise TypeError("model default registry hash must be typed")
        if not isinstance(self.provider_id, ProviderId):
            raise TypeError("model default provider must use its typed contract")
        _exact_model_id(self.model_id)
        if not isinstance(
            self.agent_configuration_revision_hash, AgentConfigurationRevisionHash
        ):
            raise TypeError("model default configuration hash must be typed")


@dataclass(frozen=True)
class ProjectModelDefaultsRevision:
    project_id: ProjectId
    revision_number: int
    defaults: tuple[ProjectModelDefault, ...]
    revision_hash: HostProjectModelDefaultsRevisionHash = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.project_id, ProjectId):
            raise TypeError("project id must use its typed contract")
        if (
            type(self.revision_number) is not int
            or not 1 <= self.revision_number <= MAXIMUM_SIGNED_INT64
        ):
            raise ValueError(
                "project model-default revision number must be a positive signed int64"
            )
        if not isinstance(self.defaults, tuple) or any(
            not isinstance(default, ProjectModelDefault) for default in self.defaults
        ):
            raise TypeError("project model defaults must use their typed contract")
        ordered = tuple(sorted(self.defaults, key=lambda default: default.difficulty))
        if len({default.difficulty for default in ordered}) != len(ordered):
            raise ValueError("project model-default difficulties must be unique")
        if len(ordered) > MAXIMUM_PROJECT_MODEL_DEFAULTS:
            raise ValueError("a project has at most three model defaults")
        object.__setattr__(self, "defaults", ordered)
        object.__setattr__(
            self,
            "revision_hash",
            HostProjectModelDefaultsRevisionHash.of(
                frame(
                    "host-project-model-defaults-revision/v1",
                    self.project_id.value.encode("utf-8"),
                    struct.pack(">Q", self.revision_number),
                    *(
                        frame(
                            "host-project-model-default/v1",
                            struct.pack(">Q", default.difficulty),
                            default.model_registry_revision_hash.value.encode("ascii"),
                            default.provider_id.value.encode("ascii"),
                            default.model_id.encode("utf-8"),
                            default.agent_configuration_revision_hash.value.encode(
                                "ascii"
                            ),
                        )
                        for default in ordered
                    ),
                )
            ),
        )


@dataclass(frozen=True)
class HostModelConfigurationSnapshot:
    """The latest registries and one project's defaults seen at one instant."""

    registries: tuple[ModelRegistryRevision, ...]
    project_defaults: ProjectModelDefaultsRevision | None

    def __post_init__(self) -> None:
        if not isinstance(self.registries, tuple) or any(
            not isinstance(registry, ModelRegistryRevision)
            for registry in self.registries
        ):
            raise TypeError("model configuration registries must use their contract")
        if self.project_defaults is not None and not isinstance(
            self.project_defaults, ProjectModelDefaultsRevision
        ):
            raise TypeError("model configuration defaults must use their contract")


class ProjectSourceConnectionConflict(Exception):
    """The same project, source kind, and revision number already hold
    different bytes."""


class ProjectSourceConnectionBytesDisagree(Exception):
    """Stored connection fields do not hash to the revision hash they carry."""


class ProjectSourceConnectionHashCollision(Exception):
    """The same connection revision hash already names different fields."""


class HostProjectSourceConnectionRevisionHash(Sha256Hash):
    """Identity of one immutable project-source connection revision."""


@dataclass(frozen=True)
class ProjectSourceId:
    """Durable identity of one source connection across its revisions."""

    value: str

    def __post_init__(self) -> None:
        if _SOURCE_ID.fullmatch(self.value) is None:
            raise ValueError("a project source id must be a canonical lowercase UUID")


def _bounded_text(value: object, maximum: int, what: str) -> str:
    if type(value) is not str or not 1 <= len(value) <= maximum:
        raise ValueError(f"{what} must contain 1..{maximum} exact characters")
    return value


@dataclass(frozen=True)
class SourceKind:
    """Which platform adapter family interprets a connection's source address.

    The value — like the address it qualifies — is the adapter's contract
    (ADR 0010 decision 1): no platform name is fixed here.
    """

    value: str

    def __post_init__(self) -> None:
        _bounded_text(self.value, MAXIMUM_SOURCE_KIND_CHARACTERS, "a source kind")


@dataclass(frozen=True)
class SourceAddress:
    """The opaque address of one project's source inside its platform.

    What the string means — a repository, a group path — is the connected
    platform adapter's contract, never reinterpreted here (the
    `TrackerItemReference` rule, applied to the source itself).
    """

    value: str

    def __post_init__(self) -> None:
        _bounded_text(self.value, MAXIMUM_SOURCE_ADDRESS_CHARACTERS, "a source address")


@dataclass(frozen=True)
class SourceReference:
    """Adapter-owned detail used to operate on a source, never its identity."""

    value: str

    def __post_init__(self) -> None:
        _bounded_text(
            self.value,
            MAXIMUM_SOURCE_REFERENCE_CHARACTERS,
            "a source reference",
        )


@dataclass(frozen=True)
class ConnectionActor:
    """The operator accountable for one connect act (ADR 0010 decision 2)."""

    value: str

    def __post_init__(self) -> None:
        _bounded_text(
            self.value, MAXIMUM_CONNECTION_ACTOR_CHARACTERS, "a connection actor"
        )


class SourceConnectionAuthMethod(StrEnum):
    """The credential method the operator chose at connect time.

    ADR 0010 decision 2 specifies two methods; the App method joins here when
    its composition is built — deferred by naming, not by a placeholder field.
    """

    PERSONAL_ACCESS_TOKEN = "personal-access-token"


class ProjectSourceConnectionLifecycle(StrEnum):
    CONNECTED = "CONNECTED"
    DISCONNECTED = "DISCONNECTED"


@dataclass(frozen=True)
class ProjectSourceConnectionRevision:
    """One immutable connect act: identities and a credential reference only.

    The credential directory is where the host resolves the secret at
    composition (ADR 0009 §6); the secret's value never enters this record,
    the channel, or any projection of either.
    """

    project_id: ProjectId
    source_id: ProjectSourceId
    revision_number: int
    source_kind: SourceKind
    source_address: SourceAddress
    credential_directory: Path
    auth_method: SourceConnectionAuthMethod
    connected_by: ConnectionActor
    lifecycle: ProjectSourceConnectionLifecycle
    connected_at: RecordedAt | None
    source_ref: SourceReference | None
    revision_hash: HostProjectSourceConnectionRevisionHash = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.project_id, ProjectId):
            raise TypeError("project id must use its typed contract")
        if not isinstance(self.source_id, ProjectSourceId):
            raise TypeError("source id must use its typed contract")
        if (
            type(self.revision_number) is not int
            or not 1 <= self.revision_number <= MAXIMUM_SIGNED_INT64
        ):
            raise ValueError(
                "project-source connection revision number must be "
                "a positive signed int64"
            )
        if not isinstance(self.source_kind, SourceKind):
            raise TypeError("source kind must use its typed contract")
        if not isinstance(self.source_address, SourceAddress):
            raise TypeError("source address must use its typed contract")
        if not isinstance(self.credential_directory, Path):
            raise TypeError("credential directory must be a path")
        if not isinstance(self.auth_method, SourceConnectionAuthMethod):
            raise TypeError("auth method must use its typed contract")
        if not isinstance(self.connected_by, ConnectionActor):
            raise TypeError("connection actor must use its typed contract")
        if not isinstance(self.lifecycle, ProjectSourceConnectionLifecycle):
            raise TypeError("connection lifecycle must use its typed contract")
        if self.connected_at is not None and not isinstance(
            self.connected_at, RecordedAt
        ):
            raise TypeError("connected at must use its typed contract")
        if self.source_ref is not None and not isinstance(
            self.source_ref, SourceReference
        ):
            raise TypeError("source reference must use its typed contract")
        stored = str(self.credential_directory)
        if not 1 <= len(stored) <= MAXIMUM_CREDENTIAL_DIRECTORY_CHARACTERS:
            raise ValueError(
                "credential directory must contain "
                f"1..{MAXIMUM_CREDENTIAL_DIRECTORY_CHARACTERS} exact characters"
            )
        object.__setattr__(self, "credential_directory", Path(stored))
        object.__setattr__(
            self,
            "revision_hash",
            HostProjectSourceConnectionRevisionHash.of(
                frame(
                    "host-project-source-connection-revision/v2",
                    self.project_id.value.encode("utf-8"),
                    self.source_id.value.encode("ascii"),
                    struct.pack(">Q", self.revision_number),
                    self.source_kind.value.encode("utf-8"),
                    self.source_address.value.encode("utf-8"),
                    stored.encode("utf-8"),
                    self.auth_method.value.encode("ascii"),
                    self.connected_by.value.encode("utf-8"),
                    self.lifecycle.value.encode("ascii"),
                    b""
                    if self.connected_at is None
                    else self.connected_at.value.encode("ascii"),
                    b""
                    if self.source_ref is None
                    else self.source_ref.value.encode("utf-8"),
                )
            ),
        )
