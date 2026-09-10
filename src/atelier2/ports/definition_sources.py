"""Reaching a definition source, and keeping what was configured about it.

Three owners, deliberately apart. The reader answers what a repository holds
at one resolved commit and knows nothing about a store. The registry answers
what is registered and what it has delivered, and nothing else -- a scan is
handed that one, so its write-freedom is the shape of what it was given rather
than a promise about what it does with more. Registering is the registrar's,
and only a caller that means to write asks for it.

Reading refuses in the source's own closed vocabulary
(`DefinitionSourceRefusal`), because every one of those refusals happens before
a transaction is opened. What a *document* is refused for is not this port's
word: the publication door already owns that sentence, and a scan passes it on
unchanged.

Taking content in is the registrar's second door, and it is one act: every
selected file is published, admitted and recorded together, or none of them is.
That is why it takes the whole batch rather than one file at a time -- a door
called once per file cannot answer for the batch, and half a taken-in source is
exactly what the operator was promised never to meet (`#660` ruled line 10).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from atelier2.contracts.catalog_v3 import (
    CatalogActivatedAt,
    CatalogActor,
    CatalogAdmissionNameHeld,
    CatalogAdmissionRetired,
    CatalogAdmissionRevisionOwned,
    CatalogLineageDisplayName,
    CatalogLineageId,
)
from atelier2.contracts.definition_sources import (
    DefinitionSourceConfiguration,
    DefinitionSourceId,
    DefinitionSourceRefusal,
    DefinitionSourceRevision,
    DefinitionSourceSelection,
    RepositoryPath,
    SourceCommit,
    SourceIntake,
)
from atelier2.contracts.revisions_v3 import (
    PublishedRevision,
    PublishedRevisionHash,
    RevisionKind,
)
from atelier2.contracts.runs import WorkflowRevision
from atelier2.ports.durable_runs import DurableStateCorrupt, DurableWriteUnavailable


class DefinitionSourceUnreadable(Exception):
    """The source could not be read, in the closed vocabulary it refuses with."""

    def __init__(self, refusal: DefinitionSourceRefusal, detail: str) -> None:
        super().__init__(f"{refusal.value}: {detail}")
        self.refusal = refusal
        self.detail = detail


@dataclass(frozen=True)
class SelectedFile:
    """One selected file as the resolved commit holds it."""

    path: RepositoryPath
    selection: DefinitionSourceSelection
    document: bytes


@dataclass(frozen=True)
class ScannedSource:
    """What one write-free read of a source found, in byte-stable path order."""

    commit: SourceCommit
    files: tuple[SelectedFile, ...]


class DefinitionSourceReader(Protocol):
    """The provider-neutral owner of what a configured source currently holds."""

    def resolve(self, configuration: DefinitionSourceConfiguration) -> SourceCommit:
        """Where the configured ref stands, reading no file of it.

        Its own door because registering answers for the location and the ref
        and for nothing else: a caller that had to scan to learn those two
        would refuse a registration for a selection problem it was never
        asked about.
        """
        ...

    def scan(self, configuration: DefinitionSourceConfiguration) -> ScannedSource:
        """Resolve the configured ref once and read every selected file of it."""
        ...


@dataclass(frozen=True)
class DefinitionSourceRegistered:
    revision: DefinitionSourceRevision


@dataclass(frozen=True)
class DefinitionSourceUnchanged:
    revision: DefinitionSourceRevision


@dataclass(frozen=True)
class DefinitionSourceFound:
    revision: DefinitionSourceRevision


@dataclass(frozen=True)
class DefinitionSourceMissing:
    source_id: DefinitionSourceId


type RegisterDefinitionSourceResult = (
    DefinitionSourceRegistered
    | DefinitionSourceUnchanged
    | DurableWriteUnavailable
    | DurableStateCorrupt
)
type ReadDefinitionSourceResult = (
    DefinitionSourceFound
    | DefinitionSourceMissing
    | DurableWriteUnavailable
    | DurableStateCorrupt
)
type ReadSourceIntakesResult = (
    Mapping[RepositoryPath, SourceIntake]
    | DurableWriteUnavailable
    | DurableStateCorrupt
)


@dataclass(frozen=True)
class SelectedWorkflow:
    """One selected workflow path, its exact bytes, and the name they are admitted under."""

    path: RepositoryPath
    revision: WorkflowRevision
    display_name: CatalogLineageDisplayName


@dataclass(frozen=True)
class SelectedDocument:
    """One selected path whose document is named by its hash alone.

    A schema or a budget policy joins no lineage and authors no name, so there
    is nothing to admit it under and nobody else's name it could collide with:
    it is published, and where it came from is recorded.
    """

    path: RepositoryPath
    revision: PublishedRevision


type SelectedIntake = SelectedWorkflow | SelectedDocument


@dataclass(frozen=True)
class PathIntaken:
    """One path whose bytes entered the catalog under this intake."""

    intake: SourceIntake


@dataclass(frozen=True)
class PathAlreadyInCatalog:
    """One path whose exact bytes the catalog already held.

    Its provenance is recorded when this path had not delivered these bytes
    before, and left alone when its latest intake already names them: a
    second row for the same delivery would record one that never happened.
    """

    path: RepositoryPath
    kind: RevisionKind
    revision_hash: PublishedRevisionHash


@dataclass(frozen=True)
class PathAdopted:
    """One path whose bytes became the new head of a lineage no source had fed.

    `#660` A3: the authored name matched a lineage a manual import founded.
    The manual revisions it already held are untouched and remain its history;
    only its head moves to this delivery, and the lineage id -- derived from
    its founding revision since decision 1 of ADR 0007 -- never changes.
    """

    intake: SourceIntake
    lineage: CatalogLineageId


type RecordedPath = PathIntaken | PathAlreadyInCatalog | PathAdopted


@dataclass(frozen=True)
class SourceIntakeRecorded:
    """One whole intake, path by path in the order it was handed over."""

    paths: tuple[RecordedPath, ...]


type SourceIntakeConflict = (
    CatalogAdmissionNameHeld | CatalogAdmissionRevisionOwned | CatalogAdmissionRetired
)
"""The catalog answers an intake can meet and only the operator can resolve.

Every other admission answer would mean the store disagrees with itself -- a
lineage that vanished between two statements of one transaction, bytes that are
unpublished a line after being published -- and is raised rather than returned,
because no operator action would make it right.
"""


@dataclass(frozen=True)
class SourceIntakeRefused:
    """The catalog would not admit one path, so no path of the batch was written."""

    path: RepositoryPath
    conflict: SourceIntakeConflict


type RecordSourceIntakesResult = (
    SourceIntakeRecorded
    | SourceIntakeRefused
    | DurableWriteUnavailable
    | DurableStateCorrupt
)


class DefinitionSourceRegistry(Protocol):
    """What is registered and what it has delivered. Nothing here writes."""

    def read_source(self, source_id: DefinitionSourceId) -> ReadDefinitionSourceResult:
        """The newest configuration this source was registered with."""
        ...

    def latest_intakes(self, source_id: DefinitionSourceId) -> ReadSourceIntakesResult:
        """The highest durable intake of every path this source has delivered."""
        ...


class DefinitionSourceRegistrar(DefinitionSourceRegistry, Protocol):
    """The registry, plus the one door that adds to it."""

    def register(
        self, configuration: DefinitionSourceConfiguration
    ) -> RegisterDefinitionSourceResult:
        """Keep this configuration under the next number, or say it already stands.

        The number is this owner's to give: it is the count of what already
        stands under that source id, which no caller can know.
        """
        ...

    def record_intakes(
        self,
        source_id: DefinitionSourceId,
        commit: SourceCommit,
        selected: tuple[SelectedIntake, ...],
        actor: CatalogActor,
        intaken_at: CatalogActivatedAt,
    ) -> RecordSourceIntakesResult:
        """Publish, admit and record every selected path, or write none of them.

        `intake_number` is this owner's to give, for the reason a revision
        number is. The instant is the caller's: a store that read a clock would
        be a second source of when something happened.
        """
        ...
