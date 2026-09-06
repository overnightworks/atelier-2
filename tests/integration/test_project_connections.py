"""Connecting a project to its source: identities recorded, credentials never.

The third family on the host configuration channel (#567, ADR 0010 decision 2):
an explicit connect appends one immutable revision, an unconnected project
answers `project-source-not-connected`, and no flow ever moves a credential
value — proven by a canary token that must not surface anywhere.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError, OperationalError

from atelier2.adapters.dbos.host_configuration import (
    DbosHostConfigurationChannel,
    append_project_root,
)
from atelier2.adapters.dbos.schema import host_project_source_connection_revisions
from atelier2.adapters.github.composition import (
    GitHubConnectionUncomposable,
    live_github_issue_source,
)
from atelier2.adapters.github.project_connections import GitHubProjectSourceConnector
from atelier2.application.project_connections import (
    ConnectionProjectUnknown,
    ConnectProjectSourceResult,
    PlatformConnectionUnknown,
    ProjectSourceConnectionMoved,
    ProjectSourceConnectionPublished,
    ProjectSourceConnectionRead,
    ProjectSourceConnectionUnchanged,
    ProjectSourceDisconnectedSuccessfully,
    UnpublishableConnection,
    connect_project_source,
    disconnect_project_source,
    get_project_source_connection,
)
from atelier2.application.project_connections import (
    ProjectSourceConnectionConflict as ApplicationProjectSourceConnectionConflict,
)
from atelier2.application.refusals import DurableStateCorrupt
from atelier2.contracts.host_configuration import (
    ConnectionActor,
    ProjectId,
    ProjectSourceConnectionLifecycle,
    ProjectSourceConnectionRevision,
    ProjectSourceId,
    SourceAddress,
    SourceConnectionAuthMethod,
    SourceKind,
    SourceReference,
)
from atelier2.host import main
from atelier2.ports.durable_runs import DurableStateCorrupt as PortDurableStateCorrupt
from atelier2.ports.host_configuration import (
    HostConfigurationReadUnavailable,
    ProjectSourceConnectionRevisionConflict,
    ProjectSourceConnectionRevisionCreated,
)
from atelier2.ports.project_connections import ProjectSourceConnector
from tests.integration.test_host_configuration import opened_channel

CANARY_TOKEN = "gho_atelier2_canary_token_must_not_appear"
GITHUB_CONNECTOR = GitHubProjectSourceConnector()


@dataclass(frozen=True)
class PublicProjectionOnly:
    def public_address(self, source_address: SourceAddress) -> str:
        prefix = "stored:"
        if not source_address.value.startswith(prefix):
            raise ValueError("the stored source address is malformed")
        return source_address.value.removeprefix(prefix)


@pytest.fixture
def connected_workshop(tmp_path: Path) -> Iterator[tuple[Engine, Path]]:
    """An opened channel with the project 'studio' rooted, plus a credential
    directory whose token file holds the canary."""

    root = tmp_path / "project"
    root.mkdir()
    credential_directory = tmp_path / "credential"
    credential_directory.mkdir()
    (credential_directory / "token").write_text(CANARY_TOKEN, encoding="utf-8")
    engine = opened_channel(tmp_path)
    append_project_root(engine, ProjectId("studio"), root)
    yield engine, credential_directory
    engine.dispose()


def _connect(
    engine: Engine,
    credential_directory: Path,
    *,
    project_id: str = "studio",
    source_kind: str = "github",
    source_address: str = "acme/studio",
    move: bool = False,
) -> ConnectProjectSourceResult:
    channel = DbosHostConfigurationChannel(engine)
    return connect_project_source(
        project_id,
        source_kind,
        source_address,
        credential_directory,
        "personal-access-token",
        "felix",
        channel,
        channel,
        move=move,
    )


def test_connect_writes_one_readable_revision(
    connected_workshop: tuple[Engine, Path],
) -> None:
    engine, credential_directory = connected_workshop

    result = _connect(engine, credential_directory)

    assert isinstance(result, ProjectSourceConnectionPublished)
    assert result.revision.revision_number == 1
    read = get_project_source_connection(
        "studio", DbosHostConfigurationChannel(engine), GITHUB_CONNECTOR
    )
    assert isinstance(read, ProjectSourceConnectionRead)
    assert read.revision == result.revision
    assert read.revision.source_kind == SourceKind("github")
    assert read.revision.source_address == SourceAddress("acme/studio")
    assert read.revision.credential_directory == credential_directory.resolve()
    assert read.revision.auth_method is (
        SourceConnectionAuthMethod.PERSONAL_ACCESS_TOKEN
    )
    assert read.revision.connected_by == ConnectionActor("felix")


def test_singular_read_uses_the_connectors_public_address_owner(
    connected_workshop: tuple[Engine, Path],
) -> None:
    engine, credential_directory = connected_workshop
    published = _connect(
        engine, credential_directory, source_address="stored:acme/studio"
    )
    assert isinstance(published, ProjectSourceConnectionPublished)

    read = get_project_source_connection(
        "studio",
        DbosHostConfigurationChannel(engine),
        cast(ProjectSourceConnector, PublicProjectionOnly()),
    )

    assert read == ProjectSourceConnectionRead(published.revision, "acme/studio")


def test_an_empty_stored_github_ref_is_refused_by_composition(
    connected_workshop: tuple[Engine, Path],
) -> None:
    engine, credential_directory = connected_workshop
    empty_ref = object.__new__(SourceReference)
    object.__setattr__(empty_ref, "value", "")
    revision = ProjectSourceConnectionRevision(
        ProjectId("studio"),
        ProjectSourceId("11111111-1111-4111-8111-111111111111"),
        1,
        SourceKind("github"),
        SourceAddress("acme/studio"),
        credential_directory,
        SourceConnectionAuthMethod.PERSONAL_ACCESS_TOKEN,
        ConnectionActor("felix"),
        ProjectSourceConnectionLifecycle.CONNECTED,
        None,
        empty_ref,
    )
    with engine.begin() as connection:
        connection.exec_driver_sql("PRAGMA ignore_check_constraints=ON")
        connection.execute(
            host_project_source_connection_revisions.insert().values(
                revision_hash=revision.revision_hash.value,
                project_id=revision.project_id.value,
                source_id=revision.source_id.value,
                source_kind=revision.source_kind.value,
                revision_number=revision.revision_number,
                source_address=revision.source_address.value,
                source_ref=empty_ref.value,
                credential_directory=str(revision.credential_directory),
                auth_method=revision.auth_method.value,
                connected_by=revision.connected_by.value,
                lifecycle=revision.lifecycle.value,
                connected_at=None,
            )
        )
        assert (
            connection.scalar(
                sa.select(host_project_source_connection_revisions.c.source_ref)
            )
            == ""
        )
    with pytest.raises(GitHubConnectionUncomposable, match="one base ref"):
        live_github_issue_source(revision)


def test_an_unconnected_project_answers_platform_connection_unknown(
    connected_workshop: tuple[Engine, Path],
) -> None:
    engine, _ = connected_workshop

    read = get_project_source_connection(
        "studio", DbosHostConfigurationChannel(engine), GITHUB_CONNECTOR
    )

    assert read == PlatformConnectionUnknown()


def test_reconnecting_the_same_source_appends_nothing(
    connected_workshop: tuple[Engine, Path],
) -> None:
    engine, credential_directory = connected_workshop
    first = _connect(engine, credential_directory)
    assert isinstance(first, ProjectSourceConnectionPublished)

    again = _connect(engine, credential_directory)

    assert again == ProjectSourceConnectionUnchanged(first.revision)
    with engine.connect() as connection:
        assert (
            connection.scalar(
                sa.select(sa.func.count()).select_from(
                    host_project_source_connection_revisions
                )
            )
            == 1
        )


@pytest.mark.proves("a-project-source-identity-never-includes-a-branch")
def test_connecting_a_different_active_source_refuses_to_retarget_it(
    connected_workshop: tuple[Engine, Path],
) -> None:
    engine, credential_directory = connected_workshop
    _connect(engine, credential_directory)

    moved = _connect(engine, credential_directory, source_address="acme/studio-mirror")

    assert moved == ApplicationProjectSourceConnectionConflict()
    read = get_project_source_connection(
        "studio", DbosHostConfigurationChannel(engine), GITHUB_CONNECTOR
    )
    assert isinstance(read, ProjectSourceConnectionRead)
    assert read.revision.source_address == SourceAddress("acme/studio")


def test_move_disconnects_the_active_source_and_connects_a_new_address(
    connected_workshop: tuple[Engine, Path],
) -> None:
    engine, credential_directory = connected_workshop
    channel = DbosHostConfigurationChannel(engine)
    first = _connect(engine, credential_directory)
    assert isinstance(first, ProjectSourceConnectionPublished)

    moved = _connect(
        engine, credential_directory, source_address="acme/studio-2", move=True
    )

    assert isinstance(moved, ProjectSourceConnectionMoved)
    assert moved.disconnected.source_id == first.revision.source_id
    assert moved.disconnected.revision_number == first.revision.revision_number + 1
    assert moved.disconnected.lifecycle is ProjectSourceConnectionLifecycle.DISCONNECTED
    assert moved.disconnected.source_address == first.revision.source_address
    assert moved.connected.source_id != first.revision.source_id
    assert moved.connected.revision_number == 1
    assert moved.connected.source_address == SourceAddress("acme/studio-2")
    assert moved.connected.lifecycle is ProjectSourceConnectionLifecycle.CONNECTED

    read = get_project_source_connection(
        "studio", DbosHostConfigurationChannel(engine), GITHUB_CONNECTOR
    )
    assert isinstance(read, ProjectSourceConnectionRead)
    assert read.revision == moved.connected

    latest_per_source = channel.latest_project_source_connection_revisions(
        ProjectId("studio")
    )
    assert isinstance(latest_per_source, tuple)
    assert set(latest_per_source) == {moved.disconnected, moved.connected}
    assert _connection_revision_count(engine) == 3
    with engine.connect() as connection:
        original_row_still_present = connection.scalar(
            sa.select(sa.func.count())
            .select_from(host_project_source_connection_revisions)
            .where(
                host_project_source_connection_revisions.c.revision_hash
                == first.revision.revision_hash.value
            )
        )
    assert original_row_still_present == 1


def test_move_to_a_previously_disconnected_address_resumes_its_own_history(
    connected_workshop: tuple[Engine, Path],
) -> None:
    engine, credential_directory = connected_workshop
    channel = DbosHostConfigurationChannel(engine)
    original = _connect(engine, credential_directory, source_address="acme/mirror")
    assert isinstance(original, ProjectSourceConnectionPublished)
    assert (
        disconnect_project_source(
            ProjectId("studio"),
            ProjectId("studio"),
            original.revision.source_id,
            channel,
            channel,
        )
        == ProjectSourceDisconnectedSuccessfully()
    )
    current = _connect(engine, credential_directory, source_address="acme/studio")
    assert isinstance(current, ProjectSourceConnectionPublished)

    moved = _connect(
        engine, credential_directory, source_address="acme/mirror", move=True
    )

    assert isinstance(moved, ProjectSourceConnectionMoved)
    assert moved.disconnected.source_id == current.revision.source_id
    assert moved.connected.source_id == original.revision.source_id
    assert moved.connected.revision_number == 3
    assert moved.connected.source_address == SourceAddress("acme/mirror")
    assert moved.connected.lifecycle is ProjectSourceConnectionLifecycle.CONNECTED


def test_move_against_the_already_active_address_is_unchanged(
    connected_workshop: tuple[Engine, Path],
) -> None:
    engine, credential_directory = connected_workshop
    first = _connect(engine, credential_directory)
    assert isinstance(first, ProjectSourceConnectionPublished)

    again = _connect(engine, credential_directory, move=True)

    assert again == ProjectSourceConnectionUnchanged(first.revision)
    assert _connection_revision_count(engine) == 1


def test_move_refuses_a_source_kind_change_and_leaves_the_old_source_connected(
    connected_workshop: tuple[Engine, Path],
) -> None:
    engine, credential_directory = connected_workshop
    first = _connect(engine, credential_directory)
    assert isinstance(first, ProjectSourceConnectionPublished)

    moved = _connect(
        engine,
        credential_directory,
        source_kind="gitlab",
        source_address="acme/studio",
        move=True,
    )

    assert moved == ApplicationProjectSourceConnectionConflict()
    read = get_project_source_connection(
        "studio", DbosHostConfigurationChannel(engine), GITHUB_CONNECTOR
    )
    assert isinstance(read, ProjectSourceConnectionRead)
    assert read.revision == first.revision
    assert read.revision.lifecycle is ProjectSourceConnectionLifecycle.CONNECTED
    assert _connection_revision_count(engine) == 1


def test_resuming_a_move_after_a_disconnect_only_crash_yields_one_connected_source(
    connected_workshop: tuple[Engine, Path],
) -> None:
    """Simulates a crash between the move's two writes: the old address is
    already `DISCONNECTED`, but the target was never written. Re-running the
    move must connect the target without leaving two active sources."""
    engine, credential_directory = connected_workshop
    channel = DbosHostConfigurationChannel(engine)
    first = _connect(engine, credential_directory)
    assert isinstance(first, ProjectSourceConnectionPublished)
    assert (
        disconnect_project_source(
            ProjectId("studio"),
            ProjectId("studio"),
            first.revision.source_id,
            channel,
            channel,
        )
        == ProjectSourceDisconnectedSuccessfully()
    )

    resumed = _connect(
        engine, credential_directory, source_address="acme/studio-2", move=True
    )

    assert isinstance(resumed, ProjectSourceConnectionPublished)
    assert resumed.revision.source_address == SourceAddress("acme/studio-2")
    latest_per_source = channel.latest_project_source_connection_revisions(
        ProjectId("studio")
    )
    assert isinstance(latest_per_source, tuple)
    connected_sources = [
        revision
        for revision in latest_per_source
        if revision.lifecycle is ProjectSourceConnectionLifecycle.CONNECTED
    ]
    assert connected_sources == [resumed.revision]
    assert len(latest_per_source) == 2
    assert _connection_revision_count(engine) == 3


def test_connecting_a_project_without_a_root_is_refused(
    connected_workshop: tuple[Engine, Path],
) -> None:
    engine, credential_directory = connected_workshop

    result = _connect(engine, credential_directory, project_id="unrooted")

    assert result == ConnectionProjectUnknown()


def test_values_that_make_no_revision_are_unpublishable(
    connected_workshop: tuple[Engine, Path],
) -> None:
    engine, credential_directory = connected_workshop
    channel = DbosHostConfigurationChannel(engine)

    result = connect_project_source(
        "studio",
        "github",
        "acme/studio",
        credential_directory,
        "a-method-nobody-declared",
        "felix",
        channel,
        channel,
    )

    assert result == UnpublishableConnection()


def test_a_different_connection_at_the_same_key_conflicts(
    connected_workshop: tuple[Engine, Path],
) -> None:
    engine, credential_directory = connected_workshop
    first = _connect(engine, credential_directory)
    assert isinstance(first, ProjectSourceConnectionPublished)
    channel = DbosHostConfigurationChannel(engine)
    rival = ProjectSourceConnectionRevision(
        ProjectId("studio"),
        first.revision.source_id,
        1,
        SourceKind("github"),
        SourceAddress("acme/other"),
        credential_directory,
        SourceConnectionAuthMethod.PERSONAL_ACCESS_TOKEN,
        ConnectionActor("felix"),
        ProjectSourceConnectionLifecycle.CONNECTED,
        None,
        None,
    )

    assert (
        channel.publish_project_source_connection_revision(rival)
        == ProjectSourceConnectionRevisionConflict()
    )


def test_latest_connection_and_cli_identity_follow_the_active_source(
    connected_workshop: tuple[Engine, Path],
) -> None:
    engine, credential_directory = connected_workshop
    channel = DbosHostConfigurationChannel(engine)
    first_source = ProjectSourceId("11111111-1111-4111-8111-111111111111")
    active_source = ProjectSourceId("22222222-2222-4222-8222-222222222222")
    first_connected = ProjectSourceConnectionRevision(
        ProjectId("studio"),
        first_source,
        1,
        SourceKind("github"),
        SourceAddress("acme/retired"),
        credential_directory,
        SourceConnectionAuthMethod.PERSONAL_ACCESS_TOKEN,
        ConnectionActor("felix"),
        ProjectSourceConnectionLifecycle.CONNECTED,
        None,
        None,
    )
    first_disconnected = ProjectSourceConnectionRevision(
        ProjectId("studio"),
        first_source,
        2,
        first_connected.source_kind,
        first_connected.source_address,
        credential_directory,
        first_connected.auth_method,
        first_connected.connected_by,
        ProjectSourceConnectionLifecycle.DISCONNECTED,
        None,
        None,
    )
    second_connected = ProjectSourceConnectionRevision(
        ProjectId("studio"),
        active_source,
        1,
        SourceKind("github"),
        SourceAddress("acme/studio"),
        credential_directory,
        SourceConnectionAuthMethod.PERSONAL_ACCESS_TOKEN,
        ConnectionActor("felix"),
        ProjectSourceConnectionLifecycle.CONNECTED,
        None,
        None,
    )
    assert channel.publish_project_source_connection_revision(
        first_connected
    ) == ProjectSourceConnectionRevisionCreated(first_connected)
    assert channel.publish_project_source_connection_revision(
        first_disconnected
    ) == ProjectSourceConnectionRevisionCreated(first_disconnected)
    assert channel.publish_project_source_connection_revision(
        second_connected
    ) == ProjectSourceConnectionRevisionCreated(second_connected)

    read = get_project_source_connection("studio", channel, GITHUB_CONNECTOR)
    assert read == ProjectSourceConnectionRead(second_connected, "acme/studio")

    result = _connect(engine, credential_directory)

    assert result == ProjectSourceConnectionUnchanged(second_connected)
    assert _connection_revision_count(engine) == 3


@pytest.mark.proves("disconnect-is-idempotent-and-every-reader-follows-lifecycle")
def test_singular_channel_read_returns_none_after_the_current_head_disconnects(
    connected_workshop: tuple[Engine, Path],
) -> None:
    engine, credential_directory = connected_workshop
    channel = DbosHostConfigurationChannel(engine)
    connected = _connect(engine, credential_directory)
    assert isinstance(connected, ProjectSourceConnectionPublished)
    assert channel.latest_project_source_connection_revision(ProjectId("studio")) == (
        connected.revision
    )
    assert (
        disconnect_project_source(
            ProjectId("studio"),
            ProjectId("studio"),
            connected.revision.source_id,
            channel,
            channel,
        )
        == ProjectSourceDisconnectedSuccessfully()
    )

    assert (
        channel.latest_project_source_connection_revision(ProjectId("studio")) is None
    )


def test_an_unreadable_project_source_store_returns_read_unavailable(
    tmp_path: Path,
) -> None:
    database = tmp_path / "atelier.sqlite"
    engine = opened_channel(tmp_path)
    engine.dispose()
    database.rename(tmp_path / "unavailable.sqlite")
    database.mkdir()

    result = DbosHostConfigurationChannel(
        engine
    ).latest_project_source_connection_revision(ProjectId("studio"))

    assert isinstance(result, HostConfigurationReadUnavailable)


def test_reading_more_than_one_active_source_is_typed_durable_corruption(
    connected_workshop: tuple[Engine, Path],
) -> None:
    engine, credential_directory = connected_workshop
    revisions = tuple(
        ProjectSourceConnectionRevision(
            ProjectId("studio"),
            source_id,
            1,
            SourceKind("github"),
            SourceAddress(source_address),
            credential_directory,
            SourceConnectionAuthMethod.PERSONAL_ACCESS_TOKEN,
            ConnectionActor("felix"),
            ProjectSourceConnectionLifecycle.CONNECTED,
            None,
            None,
        )
        for source_id, source_address in (
            (
                ProjectSourceId("11111111-1111-4111-8111-111111111111"),
                "acme/first",
            ),
            (
                ProjectSourceId("22222222-2222-4222-8222-222222222222"),
                "acme/second",
            ),
        )
    )
    with engine.begin() as connection:
        connection.execute(
            host_project_source_connection_revisions.insert(),
            tuple(
                {
                    "revision_hash": revision.revision_hash.value,
                    "project_id": revision.project_id.value,
                    "source_id": revision.source_id.value,
                    "source_kind": revision.source_kind.value,
                    "revision_number": revision.revision_number,
                    "source_address": revision.source_address.value,
                    "source_ref": None,
                    "credential_directory": str(revision.credential_directory),
                    "auth_method": revision.auth_method.value,
                    "connected_by": revision.connected_by.value,
                    "lifecycle": revision.lifecycle.value,
                    "connected_at": None,
                }
                for revision in revisions
            ),
        )

    assert (
        get_project_source_connection(
            "studio", DbosHostConfigurationChannel(engine), GITHUB_CONNECTOR
        )
        == DurableStateCorrupt()
    )
    assert (
        DbosHostConfigurationChannel(engine).publish_project_source_connection_revision(
            revisions[0]
        )
        == PortDurableStateCorrupt()
    )


def test_a_missing_v45_connection_column_is_durable_corruption(
    connected_workshop: tuple[Engine, Path],
) -> None:
    engine, _ = connected_workshop
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "ALTER TABLE host_project_source_connection_revisions "
            "RENAME COLUMN source_ref TO missing_source_ref"
        )

    channel = DbosHostConfigurationChannel(engine)

    assert (
        channel.latest_project_source_connection_revisions(ProjectId("studio"))
        == PortDurableStateCorrupt()
    )


def test_an_incomplete_v45_shape_is_corrupt_with_opaque_driver_wording(
    connected_workshop: tuple[Engine, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, _ = connected_workshop
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "ALTER TABLE host_project_source_connection_revisions "
            "RENAME COLUMN source_ref TO missing_source_ref"
        )
    monkeypatch.setattr(
        OperationalError,
        "__str__",
        lambda _error: "the driver reported an opaque operational failure",
    )

    result = DbosHostConfigurationChannel(
        engine
    ).latest_project_source_connection_revision(ProjectId("studio"))

    assert result == PortDurableStateCorrupt()


def _connection_revision_count(engine: Engine) -> int:
    with engine.connect() as connection:
        return int(
            connection.scalar(
                sa.select(sa.func.count()).select_from(
                    host_project_source_connection_revisions
                )
            )
            or 0
        )


@pytest.mark.parametrize(
    "rewrite",
    [
        pytest.param(
            host_project_source_connection_revisions.update().values(
                source_address="acme/hijacked"
            ),
            id="update",
        ),
        pytest.param(host_project_source_connection_revisions.delete(), id="delete"),
    ],
)
def test_a_connection_revision_can_no_longer_be_rewritten(
    connected_workshop: tuple[Engine, Path], rewrite: sa.Executable
) -> None:
    engine, credential_directory = connected_workshop
    _connect(engine, credential_directory)

    with (
        pytest.raises(
            IntegrityError, match="project-source connection revisions are immutable"
        ),
        engine.begin() as connection,
    ):
        connection.execute(rewrite)


def test_no_flow_and_no_stored_byte_carries_the_credential_value(
    connected_workshop: tuple[Engine, Path], tmp_path: Path
) -> None:
    """The record is a reference: the canary the credential directory holds
    must appear in no revision, no read, and no row of the whole store."""

    engine, credential_directory = connected_workshop
    result = _connect(engine, credential_directory)
    read = get_project_source_connection(
        "studio", DbosHostConfigurationChannel(engine), GITHUB_CONNECTOR
    )

    assert CANARY_TOKEN not in repr(result)
    assert CANARY_TOKEN not in repr(read)
    engine.dispose()
    with sqlite3.connect(tmp_path / "atelier.sqlite") as connection:
        full_projection = "\n".join(connection.iterdump())
    assert CANARY_TOKEN not in full_projection


def _connect_command(
    tmp_path: Path,
    credential_directory: Path,
    *,
    project_id: str = "studio",
    source_kind: str = "github",
    source_address: str = "acme/studio",
    source_ref: str | None = "main",
    move: bool = False,
) -> list[str]:
    command = [
        "connect",
        "--database",
        str(tmp_path / "atelier.sqlite"),
        "--project-id",
        project_id,
        "--source-kind",
        source_kind,
        "--source-address",
        source_address,
        "--credential-directory",
        str(credential_directory),
        "--auth-method",
        "personal-access-token",
        "--actor",
        "felix",
    ]
    if source_ref is not None:
        command.extend(("--source-ref", source_ref))
    if move:
        command.append("--move")
    return command


def test_the_connect_command_writes_the_revision_the_channel_reads_back(
    connected_workshop: tuple[Engine, Path],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    engine, credential_directory = connected_workshop
    engine.dispose()

    assert main(_connect_command(tmp_path, credential_directory)) == 0
    assert "revision 1" in capsys.readouterr().out

    assert main(_connect_command(tmp_path, credential_directory)) == 0
    assert "unchanged" in capsys.readouterr().out

    reopened = opened_channel(tmp_path)
    try:
        read = get_project_source_connection(
            "studio", DbosHostConfigurationChannel(reopened), GITHUB_CONNECTOR
        )
        assert isinstance(read, ProjectSourceConnectionRead)
        assert read.revision.revision_number == 1
        assert read.revision.source_address == SourceAddress("acme/studio")
        assert read.revision.source_ref == SourceReference("main")
    finally:
        reopened.dispose()


@pytest.mark.parametrize(
    ("source_address", "source_ref", "expected_refusal"),
    [
        pytest.param(
            "acme/studio",
            None,
            "requires a nonempty --source-ref",
            id="missing-source-ref",
        ),
        pytest.param(
            "acme/studio",
            "",
            "requires a nonempty --source-ref",
            id="empty-source-ref",
        ),
        pytest.param(
            "acme/studio@main",
            "main",
            "requires branchless owner/name",
            id="branch-in-source-address",
        ),
    ],
)
def test_the_connect_command_refuses_invalid_current_github_identity_shapes(
    connected_workshop: tuple[Engine, Path],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    source_address: str,
    source_ref: str | None,
    expected_refusal: str,
) -> None:
    engine, credential_directory = connected_workshop
    engine.dispose()

    exit_code = main(
        _connect_command(
            tmp_path,
            credential_directory,
            source_address=source_address,
            source_ref=source_ref,
        )
    )

    assert exit_code == 1
    assert expected_refusal in capsys.readouterr().err
    reopened = opened_channel(tmp_path)
    try:
        assert _connection_revision_count(reopened) == 0
    finally:
        reopened.dispose()


def test_the_connect_command_keeps_a_generic_source_ref_optional(
    connected_workshop: tuple[Engine, Path],
    tmp_path: Path,
) -> None:
    engine, credential_directory = connected_workshop
    engine.dispose()

    assert (
        main(
            _connect_command(
                tmp_path,
                credential_directory,
                source_kind="generic-tracker",
                source_ref=None,
            )
        )
        == 0
    )

    reopened = opened_channel(tmp_path)
    try:
        read = get_project_source_connection(
            "studio", DbosHostConfigurationChannel(reopened), GITHUB_CONNECTOR
        )
        assert isinstance(read, ProjectSourceConnectionRead)
        assert read.revision.source_kind == SourceKind("generic-tracker")
        assert read.revision.source_ref is None
    finally:
        reopened.dispose()


@pytest.mark.proves("a-project-source-identity-never-includes-a-branch")
def test_the_connect_command_keeps_a_github_ref_out_of_identity_and_composes_it(
    connected_workshop: tuple[Engine, Path],
    tmp_path: Path,
) -> None:
    engine, credential_directory = connected_workshop
    engine.dispose()

    assert (
        main(
            _connect_command(
                tmp_path,
                credential_directory,
                source_ref="main",
            )
        )
        == 0
    )

    reopened = opened_channel(tmp_path)
    try:
        read = get_project_source_connection(
            "studio", DbosHostConfigurationChannel(reopened), GITHUB_CONNECTOR
        )
        assert isinstance(read, ProjectSourceConnectionRead)
        assert read.revision.source_address == SourceAddress("acme/studio")
        assert read.revision.source_ref == SourceReference("main")
        composed = live_github_issue_source(read.revision)
        assert composed.repository.owner == "acme"
        assert composed.repository.name == "studio"
        assert composed.repository.base_branch == "main"
    finally:
        reopened.dispose()


@pytest.mark.proves("a-project-source-identity-never-includes-a-branch")
def test_the_connect_command_cannot_retarget_an_active_source(
    connected_workshop: tuple[Engine, Path],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    engine, credential_directory = connected_workshop
    engine.dispose()

    assert main(_connect_command(tmp_path, credential_directory)) == 0
    capsys.readouterr()

    exit_code = main(
        _connect_command(
            tmp_path,
            credential_directory,
            source_address="acme/other",
        )
    )

    assert exit_code == 1
    assert "collides with one already recorded" in capsys.readouterr().err
    reopened = opened_channel(tmp_path)
    try:
        read = get_project_source_connection(
            "studio", DbosHostConfigurationChannel(reopened), GITHUB_CONNECTOR
        )
        assert isinstance(read, ProjectSourceConnectionRead)
        assert read.revision.revision_number == 1
        assert read.revision.source_address == SourceAddress("acme/studio")
    finally:
        reopened.dispose()


def test_the_connect_command_moves_an_active_connection_to_a_new_address(
    connected_workshop: tuple[Engine, Path],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    engine, credential_directory = connected_workshop
    engine.dispose()

    assert main(_connect_command(tmp_path, credential_directory)) == 0
    capsys.readouterr()

    exit_code = main(
        _connect_command(
            tmp_path,
            credential_directory,
            source_address="acme/renamed",
            move=True,
        )
    )

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "disconnected github source 'acme/studio'" in output
    assert "revision 2" in output
    assert "connected project 'studio' to github source 'acme/renamed'" in output
    assert "revision 1" in output
    reopened = opened_channel(tmp_path)
    try:
        read = get_project_source_connection(
            "studio", DbosHostConfigurationChannel(reopened), GITHUB_CONNECTOR
        )
        assert isinstance(read, ProjectSourceConnectionRead)
        assert read.revision.source_address == SourceAddress("acme/renamed")
    finally:
        reopened.dispose()


def test_the_connect_command_refuses_an_unrooted_project(
    connected_workshop: tuple[Engine, Path],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    engine, credential_directory = connected_workshop
    engine.dispose()

    exit_code = main(
        _connect_command(tmp_path, credential_directory, project_id="unrooted")
    )

    assert exit_code == 1
    assert "project-unknown" in capsys.readouterr().err


def test_the_connect_command_does_not_create_a_store(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    credential_directory = tmp_path / "credential"
    credential_directory.mkdir()

    exit_code = main(_connect_command(tmp_path, credential_directory))

    assert exit_code == 1
    assert "does not create a store" in capsys.readouterr().err
    assert not (tmp_path / "atelier.sqlite").exists()
