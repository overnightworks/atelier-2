"""What one scan of a source becomes in the catalog, decided before anything is written.

This is the owner of two questions a store must never answer: which selected
files are taken in, and under which name each of them is admitted. It asks the
scan what the source holds -- once, so the bytes that are admitted are the bytes
the operator was shown -- and hands the whole batch to one durable door, which
writes all of it or none of it (`#660` ruled lines 10 and 18).

Every refusal this layer owns happens before that door is opened: a commit the
ref no longer stands at, a document the publication door would refuse, and an
authored name outside the catalog's name contract. The last one refuses the
batch rather than the file, because a partial intake of a commit is exactly the
half-landed pull the operator was promised never to meet.

Idempotence is not decided here. Whether these exact bytes already stand in the
catalog is durable state, and reading it here to decide would be a second
answer racing the store's own; the store reports per path what it found
(`#660` ruled line 9).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import assert_never

from atelier2.application.publish_workflow_revision import (
    PublishableWorkflow,
    WorkflowPublicationLimits,
)
from atelier2.application.refusals import (
    DurableStateCorrupt,
    ReadUnavailable,
    WriteUnavailable,
)
from atelier2.application.scan_definition_source import (
    DefinitionSourceScanned,
    DefinitionSourceUnknown,
    ScannedDocumentInvalid,
    ScanRefused,
    scan_definition_source,
)
from atelier2.contracts.catalog_v3 import (
    CatalogActivatedAt,
    CatalogActor,
    CatalogLineageDisplayName,
)
from atelier2.contracts.definition_sources import (
    DefinitionSourceId,
    DefinitionSourceRevision,
    RepositoryPath,
    SourceCommit,
)
from atelier2.contracts.revisions_v3 import PublishedRevision
from atelier2.ports.definition_sources import (
    DefinitionSourceReader,
    DefinitionSourceRegistrar,
    RecordedPath,
    SelectedDocument,
    SelectedIntake,
    SelectedWorkflow,
    SourceIntakeRecorded,
    SourceIntakeRefused,
)
from atelier2.ports.durable_runs import (
    DurableStateCorrupt as PortDurableStateCorrupt,
)
from atelier2.ports.durable_runs import DurableWriteUnavailable
from atelier2.ports.workflow_revisions import WorkflowDocumentParser


@dataclass(frozen=True)
class DefinitionSourceIntaken:
    """One commit of one source, taken into the catalog in one act."""

    revision: DefinitionSourceRevision
    commit: SourceCommit
    paths: tuple[RecordedPath, ...]


@dataclass(frozen=True)
class IntakeNameUnusable:
    """A selected file authored a name the catalog cannot hold.

    Named on its path with the name as written, because the operator fixes this
    in the repository and needs to know which file to open.
    """

    path: RepositoryPath
    name: str


@dataclass(frozen=True)
class SourcePositionMoved:
    """The ref no longer stands where the operator asked to take content in from."""

    asked: SourceCommit
    resolved: SourceCommit


type IntakeDefinitionSourceResult = (
    DefinitionSourceIntaken
    | SourceIntakeRefused
    | IntakeNameUnusable
    | SourcePositionMoved
    | ScanRefused
    | ScannedDocumentInvalid
    | DefinitionSourceUnknown
    | WriteUnavailable
    | DurableStateCorrupt
)


def intake_definition_source(
    source_id: DefinitionSourceId,
    source_position: SourceCommit | None,
    actor: CatalogActor,
    intaken_at: CatalogActivatedAt,
    sources: DefinitionSourceRegistrar,
    reader: DefinitionSourceReader,
    parser: WorkflowDocumentParser,
    limits: WorkflowPublicationLimits,
) -> IntakeDefinitionSourceResult:
    """Take one commit of a source into the catalog, whole, or take in nothing.

    `source_position` is the commit a scan showed the operator. Naming it turns
    the intake into the one they decided on: a ref that moved in between is
    refused rather than silently taken in.
    """

    scanned = scan_definition_source(source_id, sources, reader, parser, limits)
    match scanned:
        case DefinitionSourceScanned():
            pass
        case ReadUnavailable(detail):
            return WriteUnavailable(detail)
        case (
            ScanRefused()
            | ScannedDocumentInvalid()
            | DefinitionSourceUnknown()
            | DurableStateCorrupt()
        ):
            return scanned
        case _ as unreachable:
            assert_never(unreachable)
    if source_position is not None and source_position != scanned.commit:
        return SourcePositionMoved(source_position, scanned.commit)
    selected = _selected(scanned)
    if isinstance(selected, IntakeNameUnusable):
        return selected
    recorded = sources.record_intakes(
        source_id, scanned.commit, selected, actor, intaken_at
    )
    match recorded:
        case SourceIntakeRecorded(paths):
            return DefinitionSourceIntaken(scanned.revision, scanned.commit, paths)
        case SourceIntakeRefused():
            return recorded
        case DurableWriteUnavailable():
            return WriteUnavailable()
        case PortDurableStateCorrupt():
            return DurableStateCorrupt()
        case _ as unreachable:
            assert_never(unreachable)


def _selected(
    scanned: DefinitionSourceScanned,
) -> tuple[SelectedIntake, ...] | IntakeNameUnusable:
    """Every carried path ready to be admitted, or the first name that cannot be.

    Built whole before the door is opened, because what the door is handed is
    the whole commit: passing it the paths up to the first unusable name would
    take part of a commit in and record that part as the commit. A schema or a
    budget policy is named by its hash, so only a workflow can stop it here.
    """

    selected: list[SelectedIntake] = []
    for path, carried in scanned.carried.items():
        if isinstance(carried, PublishedRevision):
            selected.append(SelectedDocument(path, carried))
            continue
        display_name = _display_name(carried)
        if display_name is None:
            return IntakeNameUnusable(path, carried.graph.name)
        selected.append(SelectedWorkflow(path, carried.revision, display_name))
    return tuple(selected)


def _display_name(
    publishable: PublishableWorkflow,
) -> CatalogLineageDisplayName | None:
    """The name the document authored, when the catalog can hold it."""

    try:
        return CatalogLineageDisplayName(publishable.graph.name)
    except (TypeError, ValueError):
        return None
