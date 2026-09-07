"""Connecting a project to its external source, as this layer's decisions.

Connecting is an explicit operator act (ADR 0010 decision 2): it appends one
immutable revision binding the project to a source kind, an opaque source
address, a credential-directory reference, the chosen auth method, and the
connecting actor. The credential value never passes through here. A project
without a record answers `project-source-not-connected`.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import assert_never
from uuid import uuid4

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
from atelier2.contracts.host_configuration import (
    ConnectionActor,
    ProjectId,
    ProjectRootRevision,
    ProjectSourceConnectionLifecycle,
    ProjectSourceConnectionRevision,
    ProjectSourceId,
    ProjectUnknown,
    SourceAddress,
    SourceConnectionAuthMethod,
    SourceKind,
    SourceReference,
)
from atelier2.contracts.when import RecordedAt, recorded_instant
from atelier2.ports.durable_runs import (
    DurableStateCorrupt as PortDurableStateCorrupt,
)
from atelier2.ports.durable_runs import DurableWriteUnavailable
from atelier2.ports.host_configuration import (
    HostConfigurationChannel,
    ProjectSourceConnectionChannel,
    ProjectSourceCredentialDirectoryReferenced,
    ProjectSourceCredentialDirectoryUnreferenced,
)
from atelier2.ports.host_configuration import (
    HostConfigurationReadUnavailable as PortHostConfigurationReadUnavailable,
)
from atelier2.ports.host_configuration import (
    ProjectSourceConnectionRevisionCollision as PortConnectionRevisionCollision,
)
from atelier2.ports.host_configuration import (
    ProjectSourceConnectionRevisionConflict as PortConnectionRevisionConflict,
)
from atelier2.ports.host_configuration import (
    ProjectSourceConnectionRevisionCreated as PortConnectionRevisionCreated,
)
from atelier2.ports.host_configuration import (
    ProjectSourceConnectionRevisionExisting as PortConnectionRevisionExisting,
)
from atelier2.ports.project_connections import (
    CredentialDepositUnavailable,
    ManagedCredentialDeposit,
    ManagedProjectSourceCredentialStore,
    ParsedProjectSourceAddress,
    ProjectSourceAddressInvalid,
    ProjectSourceAuthenticationRefused,
    ProjectSourceConnector,
    ProjectSourceCredentialUnresolvable,
    ProjectSourceValidationUnavailable,
    ValidatedProjectSource,
)


@dataclass(frozen=True)
class ProjectSourceConnectionRead:
    revision: ProjectSourceConnectionRevision
    public_address: str


@dataclass(frozen=True)
class PlatformConnectionUnknown:
    """The project names no connection record (ADR 0010's refusal)."""


type GetProjectSourceConnectionResult = (
    ProjectSourceConnectionRead
    | PlatformConnectionUnknown
    | ReadUnavailable
    | DurableStateCorrupt
)
type GetServedProjectSourceConnectionResult = (
    GetProjectSourceConnectionResult | ServedProjectUnknown
)


@dataclass(frozen=True)
class ProjectSourceConnectionPublished:
    revision: ProjectSourceConnectionRevision


@dataclass(frozen=True)
class ProjectSourceConnectionUnchanged:
    revision: ProjectSourceConnectionRevision


@dataclass(frozen=True)
class ProjectSourceConnectionConflict:
    pass


@dataclass(frozen=True)
class ProjectSourceConnectionCollision:
    pass


@dataclass(frozen=True)
class ProjectSourceConnectionMoved:
    """A `--move` connect: the old address disconnected, the new one connected.

    Both revisions are published, and neither replaces the other in the
    channel's history -- the old address's row stays, now `DISCONNECTED`.
    """

    disconnected: ProjectSourceConnectionRevision
    connected: ProjectSourceConnectionRevision


@dataclass(frozen=True)
class ConnectionProjectUnknown:
    """The id is malformed, or names a project with no configured root."""


@dataclass(frozen=True)
class UnpublishableConnection:
    """The authored values do not make one connection revision."""


@dataclass(frozen=True)
class ProjectSourceSummary:
    source_id: ProjectSourceId
    source_kind: SourceKind
    public_address: str
    connected_at: RecordedAt | None
    revision_number: int
    auth_method: SourceConnectionAuthMethod


@dataclass(frozen=True)
class ProjectSourcesRead:
    sources: tuple[ProjectSourceSummary, ...]


@dataclass(frozen=True)
class ManagedProjectSourcePublished:
    source: ProjectSourceSummary


@dataclass(frozen=True)
class ProjectSourceAlreadyConnected:
    source_id: ProjectSourceId


@dataclass(frozen=True)
class ProjectSourceUnknown:
    pass


@dataclass(frozen=True)
class ProjectSourceDisconnected:
    pass


@dataclass(frozen=True)
class ProjectSourceInvalid:
    reason: str


@dataclass(frozen=True)
class ProjectSourceTokenRefused:
    reason: str


@dataclass(frozen=True)
class ProjectSourceUnavailable:
    detail: str | None = None


@dataclass(frozen=True)
class ProjectSourceDisconnectedSuccessfully:
    pass


type ListProjectSourcesResult = (
    ProjectSourcesRead | ServedProjectUnknown | ReadUnavailable | DurableStateCorrupt
)
type ConnectManagedProjectSourceResult = (
    ManagedProjectSourcePublished
    | ProjectSourceAlreadyConnected
    | ConnectionProjectUnknown
    | ProjectSourceInvalid
    | ProjectSourceTokenRefused
    | ProjectSourceUnavailable
    | WriteUnavailable
    | DurableStateCorrupt
)
type DisconnectProjectSourceResult = (
    ProjectSourceDisconnectedSuccessfully
    | ProjectSourceUnknown
    | ConnectionProjectUnknown
    | WriteUnavailable
    | DurableStateCorrupt
)
type RotateProjectSourceTokenResult = (
    ManagedProjectSourcePublished
    | ProjectSourceUnknown
    | ProjectSourceDisconnected
    | ConnectionProjectUnknown
    | ProjectSourceTokenRefused
    | ProjectSourceInvalid
    | ProjectSourceUnavailable
    | WriteUnavailable
    | DurableStateCorrupt
)


type ConnectProjectSourceResult = (
    ProjectSourceConnectionPublished
    | ProjectSourceConnectionUnchanged
    | ProjectSourceConnectionConflict
    | ProjectSourceConnectionCollision
    | ProjectSourceConnectionMoved
    | ConnectionProjectUnknown
    | UnpublishableConnection
    | WriteUnavailable
    | DurableStateCorrupt
)


def get_project_source_connection(
    project_id: str,
    connections: ProjectSourceConnectionChannel,
    connector: ProjectSourceConnector,
) -> GetProjectSourceConnectionResult:
    try:
        project = ProjectId(project_id)
    except ProjectUnknown:
        return PlatformConnectionUnknown()
    match connections.latest_project_source_connection_revision(project):
        case (
            ProjectSourceConnectionRevision(
                lifecycle=ProjectSourceConnectionLifecycle.CONNECTED
            ) as revision
        ):
            try:
                public_address = connector.public_address(revision.source_address)
            except ValueError:
                return DurableStateCorrupt()
            return ProjectSourceConnectionRead(revision, public_address)
        case ProjectSourceConnectionRevision():
            return PlatformConnectionUnknown()
        case None:
            return PlatformConnectionUnknown()
        case PortHostConfigurationReadUnavailable(detail):
            return ReadUnavailable(detail)
        case PortDurableStateCorrupt():
            return DurableStateCorrupt()
        case _ as unreachable:
            assert_never(unreachable)


def get_served_project_source_connection(
    project_id: ProjectId,
    served_project_id: ProjectId | None,
    host_configuration: HostConfigurationChannel,
    connections: ProjectSourceConnectionChannel,
    connector: ProjectSourceConnector,
) -> GetServedProjectSourceConnectionResult:
    match get_project(project_id, served_project_id, host_configuration):
        case ProjectRead():
            return get_project_source_connection(
                project_id.value, connections, connector
            )
        case ServedProjectUnknown() as unknown:
            return unknown
        case ReadUnavailable() as unavailable:
            return unavailable
        case DurableStateCorrupt() as corrupt:
            return corrupt
        case _ as unreachable:
            assert_never(unreachable)


def _known_project(
    project_id: ProjectId, channel: HostConfigurationChannel
) -> ConnectionProjectUnknown | WriteUnavailable | DurableStateCorrupt | None:
    match channel.latest_project_root_revision(project_id):
        case None:
            return ConnectionProjectUnknown()
        case ProjectRootRevision():
            return None
        case PortHostConfigurationReadUnavailable(detail):
            return WriteUnavailable(detail)
        case PortDurableStateCorrupt():
            return DurableStateCorrupt()
        case _ as unreachable:
            assert_never(unreachable)


def _unchanged_fields(
    latest: ProjectSourceConnectionRevision,
    candidate: ProjectSourceConnectionRevision,
) -> bool:
    """Whether the candidate restates `latest` apart from its number and instant."""
    return candidate == replace(
        latest,
        revision_number=candidate.revision_number,
        connected_at=candidate.connected_at,
    )


def new_project_source_id() -> ProjectSourceId:
    return ProjectSourceId(str(uuid4()))


@dataclass(frozen=True)
class _ConnectionLineage:
    """What a connect continues, and the connected source a `--move` leaves behind."""

    continued: ProjectSourceConnectionRevision | None
    moved_from: ProjectSourceConnectionRevision | None

    def connected_since(self, requested: RecordedAt | None) -> RecordedAt | None:
        """A connected source keeps its instant; a new or reconnected one takes now."""
        continued = self.continued
        if (
            continued is None
            or continued.lifecycle is ProjectSourceConnectionLifecycle.DISCONNECTED
        ):
            return requested or recorded_instant()
        return continued.connected_at


def _connection_lineage(
    latest_sources: tuple[ProjectSourceConnectionRevision, ...],
    source_kind: SourceKind,
    source_address: SourceAddress,
    move: bool,
) -> _ConnectionLineage | ProjectSourceConnectionConflict | DurableStateCorrupt:
    active = _active_source(latest_sources)
    if isinstance(active, DurableStateCorrupt):
        return active
    moved_from: ProjectSourceConnectionRevision | None = None
    if active is not None and (
        active.source_kind != source_kind or active.source_address != source_address
    ):
        if not (move and active.source_kind == source_kind):
            return ProjectSourceConnectionConflict()
        moved_from = active
        active = None
    matching_history = tuple(
        revision
        for revision in latest_sources
        if revision.lifecycle is ProjectSourceConnectionLifecycle.DISCONNECTED
        and revision.source_kind == source_kind
        and revision.source_address == source_address
    )
    if len(matching_history) > 1:
        return DurableStateCorrupt()
    continued = active or (None if not matching_history else matching_history[0])
    return _ConnectionLineage(continued, moved_from)


def connect_project_source(
    project_id: str,
    source_kind: str,
    source_address: str,
    credential_directory: Path,
    auth_method: str,
    connected_by: str,
    channel: HostConfigurationChannel,
    connections: ProjectSourceConnectionChannel,
    *,
    source_id_generator: Callable[[], ProjectSourceId] = new_project_source_id,
    connected_at: RecordedAt | None = None,
    source_ref: str | None = None,
    move: bool = False,
) -> ConnectProjectSourceResult:
    try:
        project = ProjectId(project_id)
    except ProjectUnknown:
        return ConnectionProjectUnknown()
    known = _known_project(project, channel)
    if known is not None:
        return known
    try:
        typed_source_kind = SourceKind(source_kind)
        typed_source_address = SourceAddress(source_address)
        typed_source_ref = None if source_ref is None else SourceReference(source_ref)
    except (TypeError, ValueError):
        return UnpublishableConnection()
    latest_sources = _latest_sources_for_write(project, connections)
    if not isinstance(latest_sources, tuple):
        return latest_sources
    lineage = _connection_lineage(
        latest_sources, typed_source_kind, typed_source_address, move
    )
    if not isinstance(lineage, _ConnectionLineage):
        return lineage
    latest = lineage.continued
    try:
        candidate = ProjectSourceConnectionRevision(
            project,
            source_id_generator() if latest is None else latest.source_id,
            1 if latest is None else latest.revision_number + 1,
            typed_source_kind,
            typed_source_address,
            credential_directory.expanduser().resolve(),
            SourceConnectionAuthMethod(auth_method),
            ConnectionActor(connected_by),
            ProjectSourceConnectionLifecycle.CONNECTED,
            lineage.connected_since(connected_at),
            typed_source_ref,
        )
    except (TypeError, ValueError):
        return UnpublishableConnection()
    if latest is not None and _unchanged_fields(latest, candidate):
        return ProjectSourceConnectionUnchanged(latest)
    if lineage.moved_from is None:
        return _connection_write_result(candidate, connections)
    return _moved_connection(lineage.moved_from, candidate, connections)


def _moved_connection(
    moved_from: ProjectSourceConnectionRevision,
    candidate: ProjectSourceConnectionRevision,
    connections: ProjectSourceConnectionChannel,
) -> ConnectProjectSourceResult:
    match _connection_write_result(_disconnected_after(moved_from), connections):
        case ProjectSourceConnectionPublished(
            revision
        ) | ProjectSourceConnectionUnchanged(revision):
            disconnected_from = revision
        case _ as failure:
            return failure
    match _connection_write_result(candidate, connections):
        case ProjectSourceConnectionPublished(
            revision
        ) | ProjectSourceConnectionUnchanged(revision):
            return ProjectSourceConnectionMoved(disconnected_from, revision)
        case _ as failure:
            return failure


def _connection_write_result(
    candidate: ProjectSourceConnectionRevision,
    connections: ProjectSourceConnectionChannel,
) -> ConnectProjectSourceResult:
    match connections.publish_project_source_connection_revision(candidate):
        case PortConnectionRevisionCreated(stored):
            return ProjectSourceConnectionPublished(stored)
        case PortConnectionRevisionExisting(stored):
            return ProjectSourceConnectionUnchanged(stored)
        case PortConnectionRevisionConflict():
            return ProjectSourceConnectionConflict()
        case PortConnectionRevisionCollision():
            return ProjectSourceConnectionCollision()
        case DurableWriteUnavailable():
            return WriteUnavailable()
        case PortDurableStateCorrupt():
            return DurableStateCorrupt()
        case _ as unreachable:
            assert_never(unreachable)


def _latest_sources(
    project_id: ProjectId,
    connections: ProjectSourceConnectionChannel,
) -> (
    tuple[ProjectSourceConnectionRevision, ...] | ReadUnavailable | DurableStateCorrupt
):
    match connections.latest_project_source_connection_revisions(project_id):
        case tuple() as revisions:
            return revisions
        case PortHostConfigurationReadUnavailable(detail):
            return ReadUnavailable(detail)
        case PortDurableStateCorrupt():
            return DurableStateCorrupt()
        case _ as unreachable:
            assert_never(unreachable)


def _active_source(
    revisions: tuple[ProjectSourceConnectionRevision, ...],
) -> ProjectSourceConnectionRevision | DurableStateCorrupt | None:
    active = tuple(
        revision
        for revision in revisions
        if revision.lifecycle is ProjectSourceConnectionLifecycle.CONNECTED
    )
    if len(active) > 1:
        return DurableStateCorrupt()
    return None if not active else active[0]


def _disconnected_after(
    connected: ProjectSourceConnectionRevision,
) -> ProjectSourceConnectionRevision:
    return ProjectSourceConnectionRevision(
        connected.project_id,
        connected.source_id,
        connected.revision_number + 1,
        connected.source_kind,
        connected.source_address,
        connected.credential_directory,
        connected.auth_method,
        connected.connected_by,
        ProjectSourceConnectionLifecycle.DISCONNECTED,
        connected.connected_at,
        connected.source_ref,
    )


def list_served_project_sources(
    project_id: ProjectId,
    served_project_id: ProjectId | None,
    host_configuration: HostConfigurationChannel,
    connections: ProjectSourceConnectionChannel,
    connector: ProjectSourceConnector,
) -> ListProjectSourcesResult:
    match get_project(project_id, served_project_id, host_configuration):
        case ProjectRead():
            pass
        case ServedProjectUnknown() as unknown:
            return unknown
        case ReadUnavailable() as unavailable:
            return unavailable
        case DurableStateCorrupt() as corrupt:
            return corrupt
        case _ as unreachable:
            assert_never(unreachable)
    latest = _latest_sources(project_id, connections)
    if isinstance(latest, (ReadUnavailable, DurableStateCorrupt)):
        return latest
    active = _active_source(latest)
    if isinstance(active, DurableStateCorrupt):
        return active
    if active is None:
        return ProjectSourcesRead(())
    try:
        public_address = connector.public_address(active.source_address)
    except ValueError:
        return DurableStateCorrupt()
    return ProjectSourcesRead((_source_summary(active, public_address),))


def _managed_connect_candidate(
    project_id: ProjectId,
    source_id: ProjectSourceId,
    revision_number: int,
    validated: ValidatedProjectSource,
    credential_directory: Path,
    connected_at: RecordedAt,
) -> ProjectSourceConnectionRevision:
    return ProjectSourceConnectionRevision(
        project_id,
        source_id,
        revision_number,
        validated.source_kind,
        validated.source_address,
        credential_directory.expanduser().resolve(),
        SourceConnectionAuthMethod.PERSONAL_ACCESS_TOKEN,
        ConnectionActor("http-api"),
        ProjectSourceConnectionLifecycle.CONNECTED,
        connected_at,
        validated.source_ref,
    )


def _rotated_source_candidate(
    latest: ProjectSourceConnectionRevision,
    validated: ValidatedProjectSource,
    credential_directory: Path,
) -> ProjectSourceConnectionRevision:
    return ProjectSourceConnectionRevision(
        latest.project_id,
        latest.source_id,
        latest.revision_number + 1,
        validated.source_kind,
        validated.source_address,
        credential_directory.expanduser().resolve(),
        latest.auth_method,
        latest.connected_by,
        ProjectSourceConnectionLifecycle.CONNECTED,
        latest.connected_at,
        validated.source_ref,
    )


def _source_summary(
    revision: ProjectSourceConnectionRevision, public_address: str
) -> ProjectSourceSummary:
    return ProjectSourceSummary(
        revision.source_id,
        revision.source_kind,
        public_address,
        revision.connected_at,
        revision.revision_number,
        revision.auth_method,
    )


def _discard_managed_token(
    staged: ManagedCredentialDeposit,
) -> ProjectSourceUnavailable | None:
    try:
        staged.discard()
    except (OSError, RuntimeError):
        return ProjectSourceUnavailable()
    return None


def _credential_directory_is_referenced(
    project_id: ProjectId,
    credential_directory: Path,
    connections: ProjectSourceConnectionChannel,
) -> bool | WriteUnavailable | DurableStateCorrupt:
    try:
        canonical_directory = credential_directory.expanduser().resolve()
    except OSError:
        return DurableStateCorrupt()
    match connections.project_source_credential_directory_reference(
        project_id, canonical_directory
    ):
        case ProjectSourceCredentialDirectoryReferenced():
            return True
        case ProjectSourceCredentialDirectoryUnreferenced():
            return False
        case PortHostConfigurationReadUnavailable(detail):
            return WriteUnavailable(detail)
        case PortDurableStateCorrupt():
            return DurableStateCorrupt()
        case _ as unreachable:
            assert_never(unreachable)


def _discarded[Outcome](
    staged: ManagedCredentialDeposit, outcome: Outcome, *, referenced: bool = False
) -> Outcome | ProjectSourceUnavailable:
    """Discard a token no revision references, then answer `outcome` unless that failed."""
    if referenced:
        return outcome
    return _discard_managed_token(staged) or outcome


def _served_project_refusal(
    project_id: ProjectId,
    served_project_id: ProjectId | None,
    host_configuration: HostConfigurationChannel,
) -> ConnectionProjectUnknown | WriteUnavailable | DurableStateCorrupt | None:
    match get_project(project_id, served_project_id, host_configuration):
        case ProjectRead():
            return None
        case ServedProjectUnknown():
            return ConnectionProjectUnknown()
        case ReadUnavailable(detail):
            return WriteUnavailable(detail)
        case DurableStateCorrupt() as corrupt:
            return corrupt
        case _ as unreachable:
            assert_never(unreachable)


def _latest_sources_for_write(
    project_id: ProjectId,
    connections: ProjectSourceConnectionChannel,
) -> (
    tuple[ProjectSourceConnectionRevision, ...] | WriteUnavailable | DurableStateCorrupt
):
    latest = _latest_sources(project_id, connections)
    if isinstance(latest, ReadUnavailable):
        return WriteUnavailable(latest.detail)
    return latest


def _latest_revision_by_source(
    project_id: ProjectId,
    source_id: ProjectSourceId,
    connections: ProjectSourceConnectionChannel,
) -> (
    ProjectSourceConnectionRevision
    | ProjectSourceUnknown
    | WriteUnavailable
    | DurableStateCorrupt
):
    match connections.latest_project_source_connection_revision_by_source(
        project_id, source_id
    ):
        case None:
            return ProjectSourceUnknown()
        case ProjectSourceConnectionRevision() as latest:
            return latest
        case PortHostConfigurationReadUnavailable(detail):
            return WriteUnavailable(detail)
        case PortDurableStateCorrupt():
            return DurableStateCorrupt()
        case _ as unreachable:
            assert_never(unreachable)


def _validated_with_staged_token(
    connector: ProjectSourceConnector,
    parsed: ParsedProjectSourceAddress,
    staged: ManagedCredentialDeposit,
) -> (
    ValidatedProjectSource
    | ProjectSourceAddressInvalid
    | ProjectSourceTokenRefused
    | ProjectSourceUnavailable
    | DurableStateCorrupt
):
    """The source as the staged token proves it; a token proving nothing is discarded here."""
    try:
        validation = connector.validate(parsed, staged.credential_directory)
    except OSError:
        return _discarded(
            staged, ProjectSourceUnavailable("source validation failed unexpectedly")
        )
    except (RuntimeError, TypeError, ValueError):
        return _discarded(staged, DurableStateCorrupt())
    match validation:
        case ValidatedProjectSource():
            return validation
        case ProjectSourceAuthenticationRefused(reason):
            return _discarded(staged, ProjectSourceTokenRefused(reason))
        case ProjectSourceCredentialUnresolvable():
            return _discarded(staged, ProjectSourceUnavailable())
        case ProjectSourceAddressInvalid():
            return _discarded(staged, validation)
        case ProjectSourceValidationUnavailable(detail):
            return _discarded(staged, ProjectSourceUnavailable(detail))
        case _ as unreachable:
            assert_never(unreachable)


def _written_connection(
    candidate: ProjectSourceConnectionRevision,
    connections: ProjectSourceConnectionChannel,
) -> ConnectProjectSourceResult:
    try:
        return _connection_write_result(candidate, connections)
    except OSError:
        return WriteUnavailable()
    except (TypeError, ValueError):
        return DurableStateCorrupt()


def _landed_as(
    result: ConnectProjectSourceResult, candidate: ProjectSourceConnectionRevision
) -> bool:
    return (
        isinstance(
            result, (ProjectSourceConnectionPublished, ProjectSourceConnectionUnchanged)
        )
        and result.revision == candidate
    )


def _prior_managed_source(
    latest: tuple[ProjectSourceConnectionRevision, ...],
    connector: ProjectSourceConnector,
    public_address: str,
) -> (
    ProjectSourceConnectionRevision
    | ProjectSourceAlreadyConnected
    | DurableStateCorrupt
    | None
):
    """The history this address continues: none, its own disconnected row, or a refusal."""
    active = _active_source(latest)
    if isinstance(active, DurableStateCorrupt):
        return active
    if active is not None:
        return ProjectSourceAlreadyConnected(active.source_id)
    try:
        matching_history = tuple(
            revision
            for revision in latest
            if connector.public_address(revision.source_address) == public_address
        )
    except ValueError:
        return DurableStateCorrupt()
    if len(matching_history) > 1:
        return DurableStateCorrupt()
    return None if not matching_history else matching_history[0]


def connect_managed_project_source(
    project_id: ProjectId,
    served_project_id: ProjectId | None,
    address: str,
    token: str,
    host_configuration: HostConfigurationChannel,
    connections: ProjectSourceConnectionChannel,
    connector: ProjectSourceConnector,
    token_deposits: ManagedProjectSourceCredentialStore,
    source_id_generator: Callable[[], ProjectSourceId],
    clock: Callable[[], RecordedAt],
) -> ConnectManagedProjectSourceResult:
    refused = _served_project_refusal(project_id, served_project_id, host_configuration)
    if refused is not None:
        return refused
    match connector.parse_address(address):
        case ParsedProjectSourceAddress() as parsed:
            pass
        case ProjectSourceAddressInvalid(reason):
            return ProjectSourceInvalid(reason)
        case _ as unreachable:
            assert_never(unreachable)
    latest = _latest_sources_for_write(project_id, connections)
    if not isinstance(latest, tuple):
        return latest
    prior = _prior_managed_source(latest, connector, parsed.public_address)
    if isinstance(prior, (ProjectSourceAlreadyConnected, DurableStateCorrupt)):
        return prior
    source_id = source_id_generator() if prior is None else prior.source_id
    staged = token_deposits.stage(source_id, token)
    if isinstance(staged, CredentialDepositUnavailable):
        return ProjectSourceUnavailable(staged.detail)
    validated = _validated_with_staged_token(connector, parsed, staged)
    if isinstance(validated, ProjectSourceAddressInvalid):
        return ProjectSourceInvalid(validated.reason)
    if not isinstance(validated, ValidatedProjectSource):
        return validated
    if (
        validated.source_kind != parsed.source_kind
        or validated.public_address != parsed.public_address
    ):
        return _discarded(staged, DurableStateCorrupt())
    try:
        credential_directory = staged.publish()
    except OSError:
        return _discarded(staged, ProjectSourceUnavailable())
    try:
        candidate = _managed_connect_candidate(
            project_id,
            source_id,
            1 if prior is None else prior.revision_number + 1,
            validated,
            credential_directory,
            clock(),
        )
    except (OSError, TypeError, ValueError):
        return _discarded(staged, DurableStateCorrupt())
    return _written_managed_connection(
        staged, candidate, validated.public_address, connections
    )


def _written_managed_connection(
    staged: ManagedCredentialDeposit,
    candidate: ProjectSourceConnectionRevision,
    public_address: str,
    connections: ProjectSourceConnectionChannel,
) -> ConnectManagedProjectSourceResult:
    """What stands after the write: the candidate, or whatever the channel holds instead."""
    result = _written_connection(candidate, connections)
    if _landed_as(result, candidate):
        return ManagedProjectSourcePublished(_source_summary(candidate, public_address))
    current = _latest_sources_for_write(candidate.project_id, connections)
    if not isinstance(current, tuple):
        return current
    durable_source = next(
        (revision for revision in current if revision.source_id == candidate.source_id),
        None,
    )
    if durable_source == candidate:
        return ManagedProjectSourcePublished(_source_summary(candidate, public_address))
    referenced = _credential_directory_is_referenced(
        candidate.project_id, candidate.credential_directory, connections
    )
    if not isinstance(referenced, bool):
        return referenced
    if referenced:
        return result if isinstance(result, DurableStateCorrupt) else WriteUnavailable()
    cleanup_failure = _discard_managed_token(staged)
    if cleanup_failure is not None:
        return cleanup_failure
    if (
        durable_source is not None
        and durable_source.revision_number > candidate.revision_number
    ):
        return WriteUnavailable()
    active_after_write = _active_source(current)
    if isinstance(active_after_write, DurableStateCorrupt):
        return active_after_write
    if active_after_write is not None:
        return ProjectSourceAlreadyConnected(active_after_write.source_id)
    if isinstance(result, WriteUnavailable):
        return result
    return DurableStateCorrupt()


def disconnect_project_source(
    project_id: ProjectId,
    served_project_id: ProjectId | None,
    source_id: ProjectSourceId,
    host_configuration: HostConfigurationChannel,
    connections: ProjectSourceConnectionChannel,
) -> DisconnectProjectSourceResult:
    refused = _served_project_refusal(project_id, served_project_id, host_configuration)
    if refused is not None:
        return refused
    latest = _latest_revision_by_source(project_id, source_id, connections)
    if not isinstance(latest, ProjectSourceConnectionRevision):
        return latest
    if latest.lifecycle is ProjectSourceConnectionLifecycle.DISCONNECTED:
        return ProjectSourceDisconnectedSuccessfully()
    candidate = _disconnected_after(latest)
    settled = _disconnected_by(
        _connection_write_result(candidate, connections), candidate
    )
    if settled is not None:
        return settled
    refreshed = _latest_revision_by_source(project_id, source_id, connections)
    if not isinstance(refreshed, ProjectSourceConnectionRevision):
        return refreshed
    if refreshed.lifecycle is ProjectSourceConnectionLifecycle.DISCONNECTED:
        return ProjectSourceDisconnectedSuccessfully()
    retried_candidate = _disconnected_after(refreshed)
    settled = _disconnected_by(
        _connection_write_result(retried_candidate, connections), retried_candidate
    )
    if settled is not None:
        return settled
    after_retry = _latest_revision_by_source(project_id, source_id, connections)
    if isinstance(after_retry, (WriteUnavailable, DurableStateCorrupt)):
        return after_retry
    if (
        isinstance(after_retry, ProjectSourceConnectionRevision)
        and after_retry.lifecycle is ProjectSourceConnectionLifecycle.DISCONNECTED
    ):
        return ProjectSourceDisconnectedSuccessfully()
    return WriteUnavailable()


def _disconnected_by(
    result: ConnectProjectSourceResult, candidate: ProjectSourceConnectionRevision
) -> DisconnectProjectSourceResult | None:
    """What writing the disconnected `candidate` settled; None while a conflict asks for a retry."""
    if isinstance(
        result, (ProjectSourceConnectionPublished, ProjectSourceConnectionUnchanged)
    ):
        if result.revision == candidate:
            return ProjectSourceDisconnectedSuccessfully()
        return DurableStateCorrupt()
    if isinstance(result, (WriteUnavailable, DurableStateCorrupt)):
        return result
    if isinstance(result, ProjectSourceConnectionConflict):
        return None
    return DurableStateCorrupt()


def _stored_address(
    connector: ProjectSourceConnector, latest: ProjectSourceConnectionRevision
) -> ParsedProjectSourceAddress | DurableStateCorrupt:
    try:
        parsed = connector.parse_stored_address(latest.source_address)
    except (TypeError, ValueError):
        return DurableStateCorrupt()
    if isinstance(parsed, ProjectSourceAddressInvalid):
        return DurableStateCorrupt()
    return parsed


def _rotation_keeps_the_source(
    connector: ProjectSourceConnector,
    latest: ProjectSourceConnectionRevision,
    validated: ValidatedProjectSource,
) -> bool:
    """Whether the token proved the very source the revision names."""
    try:
        stored_public_address = connector.public_address(latest.source_address)
    except ValueError:
        return False
    return (
        validated.source_kind == latest.source_kind
        and validated.public_address == stored_public_address
    )


def rotate_project_source_token(
    project_id: ProjectId,
    served_project_id: ProjectId | None,
    source_id: ProjectSourceId,
    token: str,
    host_configuration: HostConfigurationChannel,
    connections: ProjectSourceConnectionChannel,
    connector: ProjectSourceConnector,
    token_deposits: ManagedProjectSourceCredentialStore,
) -> RotateProjectSourceTokenResult:
    refused = _served_project_refusal(project_id, served_project_id, host_configuration)
    if refused is not None:
        return refused
    latest = _latest_revision_by_source(project_id, source_id, connections)
    if not isinstance(latest, ProjectSourceConnectionRevision):
        return latest
    if latest.lifecycle is ProjectSourceConnectionLifecycle.DISCONNECTED:
        return ProjectSourceDisconnected()
    parsed = _stored_address(connector, latest)
    if isinstance(parsed, DurableStateCorrupt):
        return parsed
    staged = token_deposits.stage(source_id, token)
    if isinstance(staged, CredentialDepositUnavailable):
        return ProjectSourceUnavailable(staged.detail)
    validated = _validated_with_staged_token(connector, parsed, staged)
    if isinstance(validated, ProjectSourceAddressInvalid):
        return DurableStateCorrupt()
    if not isinstance(validated, ValidatedProjectSource):
        return validated
    if not _rotation_keeps_the_source(connector, latest, validated):
        return _discarded(staged, DurableStateCorrupt())
    try:
        credential_directory = staged.publish()
    except OSError:
        return _discarded(staged, ProjectSourceUnavailable())
    try:
        candidate = _rotated_source_candidate(latest, validated, credential_directory)
    except (OSError, TypeError, ValueError):
        return _discarded(staged, DurableStateCorrupt())
    return _written_rotation(staged, candidate, validated, connector, connections)


def _written_rotation(
    staged: ManagedCredentialDeposit,
    candidate: ProjectSourceConnectionRevision,
    validated: ValidatedProjectSource,
    connector: ProjectSourceConnector,
    connections: ProjectSourceConnectionChannel,
) -> RotateProjectSourceTokenResult:
    """What stands after the write: the candidate, a retry on conflict, or what won."""
    result = _written_connection(candidate, connections)
    if _landed_as(result, candidate):
        return ManagedProjectSourcePublished(
            _source_summary(candidate, validated.public_address)
        )
    referenced = _credential_directory_is_referenced(
        candidate.project_id, candidate.credential_directory, connections
    )
    if not isinstance(referenced, bool):
        return referenced
    refreshed = _latest_revision_by_source(
        candidate.project_id, candidate.source_id, connections
    )
    if not isinstance(refreshed, ProjectSourceConnectionRevision):
        return _discarded(staged, refreshed, referenced=referenced)
    if refreshed == candidate:
        return ManagedProjectSourcePublished(
            _source_summary(refreshed, validated.public_address)
        )
    if refreshed.revision_number > candidate.revision_number:
        lost = result if isinstance(result, DurableStateCorrupt) else WriteUnavailable()
        return _discarded(staged, lost, referenced=referenced)
    if refreshed.lifecycle is ProjectSourceConnectionLifecycle.DISCONNECTED:
        return _discarded(staged, ProjectSourceDisconnected(), referenced=referenced)
    if isinstance(result, ProjectSourceConnectionConflict):
        return _retried_rotation(
            staged,
            refreshed,
            validated,
            candidate.credential_directory,
            connector,
            connections,
            referenced,
        )
    lost = result if isinstance(result, WriteUnavailable) else DurableStateCorrupt()
    return _discarded(staged, lost, referenced=referenced)


def _retried_rotation(
    staged: ManagedCredentialDeposit,
    refreshed: ProjectSourceConnectionRevision,
    validated: ValidatedProjectSource,
    credential_directory: Path,
    connector: ProjectSourceConnector,
    connections: ProjectSourceConnectionChannel,
    referenced: bool,
) -> RotateProjectSourceTokenResult:
    """One more write onto the revision that won the race, if it still names this source."""
    try:
        refreshed_public_address = connector.public_address(refreshed.source_address)
        retried_candidate = _rotated_source_candidate(
            refreshed, validated, credential_directory
        )
    except (TypeError, ValueError):
        return _discarded(staged, DurableStateCorrupt(), referenced=referenced)
    if refreshed_public_address != validated.public_address:
        return _discarded(staged, DurableStateCorrupt(), referenced=referenced)
    retried = _written_connection(retried_candidate, connections)
    if _landed_as(retried, retried_candidate):
        return ManagedProjectSourcePublished(
            _source_summary(retried_candidate, validated.public_address)
        )
    retry_referenced = _credential_directory_is_referenced(
        refreshed.project_id, credential_directory, connections
    )
    if not isinstance(retry_referenced, bool):
        return retry_referenced
    after_retry = _latest_revision_by_source(
        refreshed.project_id, refreshed.source_id, connections
    )
    if isinstance(after_retry, (WriteUnavailable, DurableStateCorrupt)):
        return _discarded(staged, after_retry, referenced=retry_referenced)
    if (
        isinstance(after_retry, ProjectSourceConnectionRevision)
        and after_retry == retried_candidate
    ):
        return ManagedProjectSourcePublished(
            _source_summary(after_retry, validated.public_address)
        )
    if retry_referenced:
        return (
            retried if isinstance(retried, DurableStateCorrupt) else WriteUnavailable()
        )
    conflicted = isinstance(
        retried, (ProjectSourceConnectionConflict, WriteUnavailable)
    )
    lost = WriteUnavailable() if conflicted else DurableStateCorrupt()
    return _discarded(staged, lost, referenced=retry_referenced)
