"""Connecting a project to its external source, as this layer's decisions.

Connecting is an explicit operator act (ADR 0010 decision 2): it appends one
immutable revision binding the project to a source kind, an opaque source
address, a credential-directory reference, the chosen auth method, and the
connecting actor. The credential value never passes through here. A project
without a record answers `project-source-not-connected`.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
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
    return (
        latest.source_kind,
        latest.source_address,
        latest.credential_directory,
        latest.auth_method,
        latest.connected_by,
        latest.lifecycle,
        latest.source_ref,
    ) == (
        candidate.source_kind,
        candidate.source_address,
        candidate.credential_directory,
        candidate.auth_method,
        candidate.connected_by,
        candidate.lifecycle,
        candidate.source_ref,
    )


def new_project_source_id() -> ProjectSourceId:
    return ProjectSourceId(str(uuid4()))


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
    latest_sources = _latest_sources(project, connections)
    if isinstance(latest_sources, ReadUnavailable):
        return WriteUnavailable(latest_sources.detail)
    if isinstance(latest_sources, DurableStateCorrupt):
        return latest_sources
    active = _active_source(latest_sources)
    if isinstance(active, DurableStateCorrupt):
        return active
    move_source: ProjectSourceConnectionRevision | None = None
    if active is not None and (
        active.source_kind != typed_source_kind
        or active.source_address != typed_source_address
    ):
        if not (move and active.source_kind == typed_source_kind):
            return ProjectSourceConnectionConflict()
        move_source = active
        active = None
    matching_history = tuple(
        revision
        for revision in latest_sources
        if revision.lifecycle is ProjectSourceConnectionLifecycle.DISCONNECTED
        and revision.source_kind == typed_source_kind
        and revision.source_address == typed_source_address
    )
    if len(matching_history) > 1:
        return DurableStateCorrupt()
    latest = active or (None if not matching_history else matching_history[0])
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
            (
                connected_at or recorded_instant()
                if latest is None
                or latest.lifecycle is ProjectSourceConnectionLifecycle.DISCONNECTED
                else latest.connected_at
            ),
            typed_source_ref,
        )
    except (TypeError, ValueError):
        return UnpublishableConnection()
    if latest is not None and _unchanged_fields(latest, candidate):
        return ProjectSourceConnectionUnchanged(latest)
    if move_source is None:
        return _connection_write_result(candidate, connections)
    match _connection_write_result(_disconnected_after(move_source), connections):
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
    summaries: list[ProjectSourceSummary] = []
    try:
        if active is not None:
            summaries.append(
                ProjectSourceSummary(
                    active.source_id,
                    active.source_kind,
                    connector.public_address(active.source_address),
                    active.connected_at,
                    active.revision_number,
                    active.auth_method,
                )
            )
    except ValueError:
        return DurableStateCorrupt()
    return ProjectSourcesRead(tuple(summaries))


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
) -> bool | ReadUnavailable | DurableStateCorrupt:
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
            return ReadUnavailable(detail)
        case PortDurableStateCorrupt():
            return DurableStateCorrupt()
        case _ as unreachable:
            assert_never(unreachable)


def _discard_if_unreferenced(
    staged: ManagedCredentialDeposit, referenced: bool
) -> ProjectSourceUnavailable | None:
    return None if referenced else _discard_managed_token(staged)


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
    match get_project(project_id, served_project_id, host_configuration):
        case ProjectRead():
            pass
        case ServedProjectUnknown():
            return ConnectionProjectUnknown()
        case ReadUnavailable(detail):
            return WriteUnavailable(detail)
        case DurableStateCorrupt() as corrupt:
            return corrupt
        case _ as unreachable:
            assert_never(unreachable)
    match connector.parse_address(address):
        case ParsedProjectSourceAddress() as parsed:
            pass
        case ProjectSourceAddressInvalid(reason):
            return ProjectSourceInvalid(reason)
        case _ as unreachable:
            assert_never(unreachable)
    latest = _latest_sources(project_id, connections)
    if isinstance(latest, ReadUnavailable):
        return WriteUnavailable(latest.detail)
    if isinstance(latest, DurableStateCorrupt):
        return latest
    try:
        active = _active_source(latest)
        if isinstance(active, DurableStateCorrupt):
            return active
        if active is not None:
            return ProjectSourceAlreadyConnected(active.source_id)
        matching_history = tuple(
            revision
            for revision in latest
            if connector.public_address(revision.source_address)
            == parsed.public_address
        )
    except ValueError:
        return DurableStateCorrupt()
    if len(matching_history) > 1:
        return DurableStateCorrupt()
    prior = None if not matching_history else matching_history[0]
    source_id = source_id_generator() if prior is None else prior.source_id
    staged = token_deposits.stage(source_id, token)
    if isinstance(staged, CredentialDepositUnavailable):
        return ProjectSourceUnavailable(staged.detail)
    try:
        validation = connector.validate(parsed, staged.credential_directory)
    except OSError:
        cleanup_failure = _discard_managed_token(staged)
        return cleanup_failure or ProjectSourceUnavailable(
            "source validation failed unexpectedly"
        )
    except (RuntimeError, TypeError, ValueError):
        cleanup_failure = _discard_managed_token(staged)
        return cleanup_failure or DurableStateCorrupt()
    match validation:
        case ValidatedProjectSource() as validated:
            pass
        case ProjectSourceAuthenticationRefused(reason):
            cleanup_failure = _discard_managed_token(staged)
            if cleanup_failure is not None:
                return cleanup_failure
            return ProjectSourceTokenRefused(reason)
        case ProjectSourceCredentialUnresolvable():
            cleanup_failure = _discard_managed_token(staged)
            return cleanup_failure or ProjectSourceUnavailable()
        case ProjectSourceAddressInvalid(reason):
            cleanup_failure = _discard_managed_token(staged)
            if cleanup_failure is not None:
                return cleanup_failure
            return ProjectSourceInvalid(reason)
        case ProjectSourceValidationUnavailable(detail):
            cleanup_failure = _discard_managed_token(staged)
            if cleanup_failure is not None:
                return cleanup_failure
            return ProjectSourceUnavailable(detail)
        case _ as unreachable:
            assert_never(unreachable)
    if (
        validated.source_kind != parsed.source_kind
        or validated.public_address != parsed.public_address
    ):
        cleanup_failure = _discard_managed_token(staged)
        return cleanup_failure or DurableStateCorrupt()
    try:
        credential_directory = staged.publish()
    except OSError:
        cleanup_failure = _discard_managed_token(staged)
        return cleanup_failure or ProjectSourceUnavailable()
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
        cleanup_failure = _discard_managed_token(staged)
        return cleanup_failure or DurableStateCorrupt()
    try:
        result = _connection_write_result(candidate, connections)
    except OSError:
        result = WriteUnavailable()
    except (TypeError, ValueError):
        result = DurableStateCorrupt()
    if (
        isinstance(
            result, (ProjectSourceConnectionPublished, ProjectSourceConnectionUnchanged)
        )
        and result.revision == candidate
    ):
        return ManagedProjectSourcePublished(
            _source_summary(result.revision, validated.public_address),
        )
    after_write = _latest_sources(project_id, connections)
    if isinstance(after_write, ReadUnavailable):
        return WriteUnavailable(after_write.detail)
    if isinstance(after_write, DurableStateCorrupt):
        return after_write
    durable_source = next(
        (revision for revision in after_write if revision.source_id == source_id), None
    )
    if durable_source is not None and durable_source == candidate:
        return ManagedProjectSourcePublished(
            _source_summary(durable_source, validated.public_address)
        )
    referenced = _credential_directory_is_referenced(
        project_id, candidate.credential_directory, connections
    )
    if isinstance(referenced, ReadUnavailable):
        return WriteUnavailable(referenced.detail)
    if isinstance(referenced, DurableStateCorrupt):
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
    active_after_write = _active_source(after_write)
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
    match get_project(project_id, served_project_id, host_configuration):
        case ProjectRead():
            pass
        case ServedProjectUnknown():
            return ConnectionProjectUnknown()
        case ReadUnavailable(detail):
            return WriteUnavailable(detail)
        case DurableStateCorrupt() as corrupt:
            return corrupt
        case _ as unreachable:
            assert_never(unreachable)
    match connections.latest_project_source_connection_revision_by_source(
        project_id, source_id
    ):
        case None:
            return ProjectSourceUnknown()
        case ProjectSourceConnectionRevision() as latest:
            pass
        case PortHostConfigurationReadUnavailable(detail):
            return WriteUnavailable(detail)
        case PortDurableStateCorrupt():
            return DurableStateCorrupt()
        case _ as unreachable:
            assert_never(unreachable)
    if latest.lifecycle is ProjectSourceConnectionLifecycle.DISCONNECTED:
        return ProjectSourceDisconnectedSuccessfully()

    candidate = _disconnected_after(latest)
    result = _connection_write_result(candidate, connections)
    if isinstance(
        result, (ProjectSourceConnectionPublished, ProjectSourceConnectionUnchanged)
    ):
        return (
            ProjectSourceDisconnectedSuccessfully()
            if result.revision == candidate
            and result.revision.lifecycle
            is ProjectSourceConnectionLifecycle.DISCONNECTED
            else DurableStateCorrupt()
        )
    if isinstance(result, WriteUnavailable):
        return result
    if isinstance(result, DurableStateCorrupt):
        return result
    if not isinstance(result, ProjectSourceConnectionConflict):
        return DurableStateCorrupt()
    match connections.latest_project_source_connection_revision_by_source(
        project_id, source_id
    ):
        case ProjectSourceConnectionRevision(
            lifecycle=ProjectSourceConnectionLifecycle.DISCONNECTED
        ):
            return ProjectSourceDisconnectedSuccessfully()
        case ProjectSourceConnectionRevision() as refreshed:
            pass
        case None:
            return ProjectSourceUnknown()
        case PortHostConfigurationReadUnavailable(detail):
            return WriteUnavailable(detail)
        case PortDurableStateCorrupt():
            return DurableStateCorrupt()
        case _ as unreachable:
            assert_never(unreachable)
    retried_candidate = _disconnected_after(refreshed)
    retried = _connection_write_result(retried_candidate, connections)
    if isinstance(
        retried, (ProjectSourceConnectionPublished, ProjectSourceConnectionUnchanged)
    ):
        return (
            ProjectSourceDisconnectedSuccessfully()
            if retried.revision == retried_candidate
            and retried.revision.lifecycle
            is ProjectSourceConnectionLifecycle.DISCONNECTED
            else DurableStateCorrupt()
        )
    if isinstance(retried, WriteUnavailable):
        return retried
    if isinstance(retried, DurableStateCorrupt):
        return retried
    if isinstance(retried, ProjectSourceConnectionConflict):
        match connections.latest_project_source_connection_revision_by_source(
            project_id, source_id
        ):
            case ProjectSourceConnectionRevision(
                lifecycle=ProjectSourceConnectionLifecycle.DISCONNECTED
            ):
                return ProjectSourceDisconnectedSuccessfully()
            case PortHostConfigurationReadUnavailable(detail):
                return WriteUnavailable(detail)
            case PortDurableStateCorrupt():
                return DurableStateCorrupt()
            case ProjectSourceConnectionRevision() | None:
                return WriteUnavailable()
            case _ as unreachable:
                assert_never(unreachable)
    return DurableStateCorrupt()


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
    match get_project(project_id, served_project_id, host_configuration):
        case ProjectRead():
            pass
        case ServedProjectUnknown():
            return ConnectionProjectUnknown()
        case ReadUnavailable(detail):
            return WriteUnavailable(detail)
        case DurableStateCorrupt() as corrupt:
            return corrupt
        case _ as unreachable:
            assert_never(unreachable)
    match connections.latest_project_source_connection_revision_by_source(
        project_id, source_id
    ):
        case None:
            return ProjectSourceUnknown()
        case ProjectSourceConnectionRevision() as latest:
            pass
        case PortHostConfigurationReadUnavailable(detail):
            return WriteUnavailable(detail)
        case PortDurableStateCorrupt():
            return DurableStateCorrupt()
        case _ as unreachable:
            assert_never(unreachable)
    if latest.lifecycle is ProjectSourceConnectionLifecycle.DISCONNECTED:
        return ProjectSourceDisconnected()
    try:
        parsed = connector.parse_stored_address(latest.source_address)
    except (TypeError, ValueError):
        return DurableStateCorrupt()
    if isinstance(parsed, ProjectSourceAddressInvalid):
        return DurableStateCorrupt()
    staged = token_deposits.stage(source_id, token)
    if isinstance(staged, CredentialDepositUnavailable):
        return ProjectSourceUnavailable(staged.detail)
    try:
        validation = connector.validate(parsed, staged.credential_directory)
    except OSError:
        cleanup_failure = _discard_managed_token(staged)
        return cleanup_failure or ProjectSourceUnavailable(
            "source validation failed unexpectedly"
        )
    except (RuntimeError, TypeError, ValueError):
        cleanup_failure = _discard_managed_token(staged)
        return cleanup_failure or DurableStateCorrupt()
    match validation:
        case ValidatedProjectSource() as validated:
            pass
        case ProjectSourceAuthenticationRefused(reason):
            cleanup_failure = _discard_managed_token(staged)
            if cleanup_failure is not None:
                return cleanup_failure
            return ProjectSourceTokenRefused(reason)
        case ProjectSourceCredentialUnresolvable():
            cleanup_failure = _discard_managed_token(staged)
            return cleanup_failure or ProjectSourceUnavailable()
        case ProjectSourceAddressInvalid():
            cleanup_failure = _discard_managed_token(staged)
            return cleanup_failure or DurableStateCorrupt()
        case ProjectSourceValidationUnavailable(detail):
            cleanup_failure = _discard_managed_token(staged)
            if cleanup_failure is not None:
                return cleanup_failure
            return ProjectSourceUnavailable(detail)
        case _ as unreachable:
            assert_never(unreachable)
    try:
        stored_public_address = connector.public_address(latest.source_address)
    except ValueError:
        cleanup_failure = _discard_managed_token(staged)
        return cleanup_failure or DurableStateCorrupt()
    if (
        validated.source_kind != latest.source_kind
        or validated.public_address != stored_public_address
    ):
        cleanup_failure = _discard_managed_token(staged)
        return cleanup_failure or DurableStateCorrupt()
    try:
        credential_directory = staged.publish()
    except OSError:
        cleanup_failure = _discard_managed_token(staged)
        return cleanup_failure or ProjectSourceUnavailable()
    try:
        candidate = _rotated_source_candidate(latest, validated, credential_directory)
    except (OSError, TypeError, ValueError):
        cleanup_failure = _discard_managed_token(staged)
        return cleanup_failure or DurableStateCorrupt()
    try:
        result = _connection_write_result(candidate, connections)
    except OSError:
        result = WriteUnavailable()
    except (TypeError, ValueError):
        result = DurableStateCorrupt()
    if (
        isinstance(
            result, (ProjectSourceConnectionPublished, ProjectSourceConnectionUnchanged)
        )
        and result.revision == candidate
    ):
        return ManagedProjectSourcePublished(
            _source_summary(result.revision, validated.public_address),
        )
    referenced = _credential_directory_is_referenced(
        project_id, credential_directory, connections
    )
    if isinstance(referenced, ReadUnavailable):
        return WriteUnavailable(referenced.detail)
    if isinstance(referenced, DurableStateCorrupt):
        return referenced
    match connections.latest_project_source_connection_revision_by_source(
        project_id, source_id
    ):
        case ProjectSourceConnectionRevision() as refreshed:
            pass
        case None:
            cleanup_failure = _discard_if_unreferenced(staged, referenced)
            if cleanup_failure is not None:
                return cleanup_failure
            return ProjectSourceUnknown()
        case PortHostConfigurationReadUnavailable(detail):
            cleanup_failure = _discard_if_unreferenced(staged, referenced)
            if cleanup_failure is not None:
                return cleanup_failure
            return WriteUnavailable(detail)
        case PortDurableStateCorrupt():
            cleanup_failure = _discard_if_unreferenced(staged, referenced)
            if cleanup_failure is not None:
                return cleanup_failure
            return DurableStateCorrupt()
        case _ as unreachable:
            assert_never(unreachable)
    if refreshed == candidate:
        return ManagedProjectSourcePublished(
            _source_summary(refreshed, validated.public_address)
        )
    if refreshed.revision_number > candidate.revision_number:
        cleanup_failure = _discard_if_unreferenced(staged, referenced)
        if cleanup_failure is not None:
            return cleanup_failure
        return result if isinstance(result, DurableStateCorrupt) else WriteUnavailable()
    if refreshed.lifecycle is ProjectSourceConnectionLifecycle.DISCONNECTED:
        cleanup_failure = _discard_if_unreferenced(staged, referenced)
        if cleanup_failure is not None:
            return cleanup_failure
        return ProjectSourceDisconnected()
    if isinstance(result, ProjectSourceConnectionConflict):
        try:
            refreshed_public_address = connector.public_address(
                refreshed.source_address
            )
            retried_candidate = _rotated_source_candidate(
                refreshed, validated, credential_directory
            )
        except (TypeError, ValueError):
            cleanup_failure = _discard_if_unreferenced(staged, referenced)
            return cleanup_failure or DurableStateCorrupt()
        if refreshed_public_address != validated.public_address:
            cleanup_failure = _discard_if_unreferenced(staged, referenced)
            return cleanup_failure or DurableStateCorrupt()
        try:
            retried = _connection_write_result(retried_candidate, connections)
        except OSError:
            retried = WriteUnavailable()
        except (TypeError, ValueError):
            retried = DurableStateCorrupt()
        if (
            isinstance(
                retried,
                (ProjectSourceConnectionPublished, ProjectSourceConnectionUnchanged),
            )
            and retried.revision == retried_candidate
        ):
            return ManagedProjectSourcePublished(
                _source_summary(retried.revision, validated.public_address)
            )
        retry_referenced = _credential_directory_is_referenced(
            project_id, credential_directory, connections
        )
        if isinstance(retry_referenced, ReadUnavailable):
            return WriteUnavailable(retry_referenced.detail)
        if isinstance(retry_referenced, DurableStateCorrupt):
            return retry_referenced
        match connections.latest_project_source_connection_revision_by_source(
            project_id, source_id
        ):
            case ProjectSourceConnectionRevision() as after_retry:
                if after_retry == retried_candidate:
                    return ManagedProjectSourcePublished(
                        _source_summary(after_retry, validated.public_address)
                    )
            case None:
                pass
            case PortHostConfigurationReadUnavailable(detail):
                cleanup_failure = _discard_if_unreferenced(staged, retry_referenced)
                if cleanup_failure is not None:
                    return cleanup_failure
                return WriteUnavailable(detail)
            case PortDurableStateCorrupt():
                cleanup_failure = _discard_if_unreferenced(staged, retry_referenced)
                if cleanup_failure is not None:
                    return cleanup_failure
                return DurableStateCorrupt()
            case _ as unreachable:
                assert_never(unreachable)
        if retry_referenced:
            return (
                retried
                if isinstance(retried, DurableStateCorrupt)
                else WriteUnavailable()
            )
        cleanup_failure = _discard_if_unreferenced(staged, retry_referenced)
        if cleanup_failure is not None:
            return cleanup_failure
        return (
            WriteUnavailable()
            if isinstance(retried, (ProjectSourceConnectionConflict, WriteUnavailable))
            else DurableStateCorrupt()
        )
    cleanup_failure = _discard_if_unreferenced(staged, referenced)
    if cleanup_failure is not None:
        return cleanup_failure
    if isinstance(result, WriteUnavailable):
        return result
    return result if isinstance(result, DurableStateCorrupt) else DurableStateCorrupt()
