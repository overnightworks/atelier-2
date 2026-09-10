"""What a registered source holds right now, compared against what came in.

A scan writes nothing. It resolves the configured ref to one commit, reads
every selected file of that commit, and says per path whether the catalog
already holds those bytes. That is the whole of it: the operator sees a newer
version exists and decides, and no commit ever enters the catalog because
somebody looked (`#660` ruled lines 2 and 12).

Every selected file is put through the reader of the kind its selection
configured -- the workflow publication door, or the schema or budget reader --
the same reader an intake would use, so a scan already says what an intake
would refuse instead of discovering it halfway through the batch. The refusal
is repeated in that reader's own words rather than renamed here.

That it writes nothing is the shape of what it is handed: the durable side it
takes is `DefinitionSourceRegistry`, which has no door that writes.

What it validated is part of what it answers, because an intake of the scanned
commit needs exactly those bytes. Reaching for them again would read a source
that may have moved between the two reads, and would publish a commit nobody
was shown.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import assert_never

from atelier2.application.publish_workflow_revision import (
    PublicationInvalid,
    PublishableWorkflow,
    WorkflowPublicationLimits,
    read_publishable_workflow,
)
from atelier2.application.refusals import DurableStateCorrupt, ReadUnavailable
from atelier2.contracts.budgets_v3 import (
    BudgetRevisionRefused,
    BudgetRevisionVerdict,
    read_budget_revision_document,
)
from atelier2.contracts.definition_sources import (
    DefinitionSourceId,
    DefinitionSourceRefusal,
    DefinitionSourceRevision,
    RepositoryPath,
    SourceCommit,
    SourceIntake,
)
from atelier2.contracts.revisions_v3 import (
    PublishedRevision,
    PublishedRevisionHash,
    RevisionKind,
)
from atelier2.contracts.schemas_v3 import (
    SchemaDocumentVerdict,
    SchemaRefused,
    read_schema_document,
)
from atelier2.contracts.workflow_refusals import WorkflowRefusal
from atelier2.ports.definition_sources import (
    DefinitionSourceFound,
    DefinitionSourceMissing,
    DefinitionSourceReader,
    DefinitionSourceRegistry,
    DefinitionSourceUnreadable,
    SelectedFile,
)
from atelier2.ports.durable_runs import (
    DurableStateCorrupt as PortDurableStateCorrupt,
)
from atelier2.ports.durable_runs import DurableWriteUnavailable
from atelier2.ports.workflow_revisions import WorkflowDocumentParser

type CarriedDocument = PublishableWorkflow | PublishedRevision
"""What a scan validated for one path: a workflow the publication door accepted,
or a hash-named document its own kind's reader accepted."""

_DOCUMENT_READERS: Mapping[
    RevisionKind, Callable[[bytes], SchemaDocumentVerdict | BudgetRevisionVerdict]
] = {
    RevisionKind.SCHEMA: read_schema_document,
    RevisionKind.BUDGET_POLICY: read_budget_revision_document,
}


class PathFreshness(StrEnum):
    """How one selected path stands between the source and the catalog."""

    IN_SYNC = "in_sync"
    SOURCE_AHEAD = "source_ahead"
    SOURCE_ABSENT = "source_absent"


@dataclass(frozen=True)
class ScannedPath:
    """One path of the resolved commit, and where the catalog stands on it.

    `revision_hash` is the identity the bytes would publish under and is absent
    exactly when the source no longer carries the path: `source_absent` says
    the catalog holds a history the source stopped serving, and inventing a
    hash for a file that is not there would be a lie about what was read.
    """

    path: RepositoryPath
    kind: RevisionKind
    freshness: PathFreshness
    revision_hash: PublishedRevisionHash | None


@dataclass(frozen=True)
class DefinitionSourceScanned:
    """One write-free reading of a registered source.

    `carried` holds every path the commit serves, validated once, in the order
    the source served them. A path the source stopped carrying is in `paths`
    and not here: there are no bytes to hold for it.
    """

    revision: DefinitionSourceRevision
    commit: SourceCommit
    paths: tuple[ScannedPath, ...]
    carried: Mapping[RepositoryPath, CarriedDocument]


@dataclass(frozen=True)
class ScanRefused:
    """The source could not be read, in its own closed vocabulary."""

    refusal: DefinitionSourceRefusal
    detail: str


@dataclass(frozen=True)
class ScannedDocumentInvalid:
    """A selected file would not pass the door an intake puts it through."""

    path: RepositoryPath
    detail: str
    refusal: WorkflowRefusal | None


@dataclass(frozen=True)
class DefinitionSourceUnknown:
    """No source is registered under this id."""

    source_id: DefinitionSourceId


type ScanDefinitionSourceResult = (
    DefinitionSourceScanned
    | ScanRefused
    | ScannedDocumentInvalid
    | DefinitionSourceUnknown
    | ReadUnavailable
    | DurableStateCorrupt
)


def scan_definition_source(
    source_id: DefinitionSourceId,
    sources: DefinitionSourceRegistry,
    reader: DefinitionSourceReader,
    parser: WorkflowDocumentParser,
    limits: WorkflowPublicationLimits,
) -> ScanDefinitionSourceResult:
    """Say where the source stands, having written nothing to reach the answer."""

    registered = sources.read_source(source_id)
    match registered:
        case DefinitionSourceMissing(missing):
            return DefinitionSourceUnknown(missing)
        case DurableWriteUnavailable():
            return ReadUnavailable()
        case PortDurableStateCorrupt():
            return DurableStateCorrupt()
        case DefinitionSourceFound(revision):
            pass
        case _ as unreachable:
            assert_never(unreachable)
    try:
        scanned = reader.scan(revision.configuration)
    except DefinitionSourceUnreadable as refused:
        return ScanRefused(refused.refusal, refused.detail)
    carried = _validated(scanned.files, parser, limits)
    if isinstance(carried, ScannedDocumentInvalid):
        return carried
    intaken = sources.latest_intakes(source_id)
    if isinstance(intaken, DurableWriteUnavailable):
        return ReadUnavailable()
    if isinstance(intaken, PortDurableStateCorrupt):
        return DurableStateCorrupt()
    return DefinitionSourceScanned(
        revision,
        scanned.commit,
        _compared(scanned.files, carried, intaken),
        carried,
    )


def _validated(
    files: tuple[SelectedFile, ...],
    parser: WorkflowDocumentParser,
    limits: WorkflowPublicationLimits,
) -> Mapping[RepositoryPath, CarriedDocument] | ScannedDocumentInvalid:
    """Every selected file put through the intake door, or the first refusal.

    The whole scan stops at one refused file rather than reporting the rest:
    an intake of this commit would refuse the batch whole, and a scan that
    listed the other paths as ready would promise something the intake door
    will not do.
    """

    read: dict[RepositoryPath, CarriedDocument] = {}
    for selected in files:
        carried = _read(selected, parser, limits)
        if isinstance(carried, ScannedDocumentInvalid):
            return carried
        read[selected.path] = carried
    return read


def _read(
    selected: SelectedFile,
    parser: WorkflowDocumentParser,
    limits: WorkflowPublicationLimits,
) -> CarriedDocument | ScannedDocumentInvalid:
    """One file through the reader of the kind its selection configured.

    A schema or budget refusal carries no workflow refusal: its reader's own
    sentence is the whole of what it says.
    """

    kind = selected.selection.kind
    if kind is RevisionKind.WORKFLOW:
        publishable = read_publishable_workflow(selected.document, parser, limits)
        if isinstance(publishable, PublicationInvalid):
            return ScannedDocumentInvalid(
                selected.path, publishable.detail, publishable.refusal
            )
        return publishable
    verdict = _DOCUMENT_READERS[kind](selected.document)
    if isinstance(verdict, (SchemaRefused, BudgetRevisionRefused)):
        return ScannedDocumentInvalid(selected.path, str(verdict), None)
    return PublishedRevision(kind, selected.document)


def published_hash(carried: CarriedDocument) -> PublishedRevisionHash:
    """The catalog identity of bytes their reader already accepted.

    One derivation for every kind a scan compares, so one file can never be
    named by two hashes.
    """

    if isinstance(carried, PublishedRevision):
        return carried.revision_hash
    return PublishedRevisionHash(carried.revision.revision_hash.value)


def _compared(
    files: tuple[SelectedFile, ...],
    carried: Mapping[RepositoryPath, CarriedDocument],
    intaken: Mapping[RepositoryPath, SourceIntake],
) -> tuple[ScannedPath, ...]:
    """Every path the source carries, then every path only the catalog holds.

    A path the source stopped serving is reported, never retired: what came in
    stays what it was, and only the operator decides what that means
    (`#660` ruled lines 7 and 17).
    """

    served = tuple(
        ScannedPath(
            selected.path,
            selected.selection.kind,
            _freshness(
                selected.selection.kind,
                published_hash(carried[selected.path]),
                intaken.get(selected.path),
            ),
            published_hash(carried[selected.path]),
        )
        for selected in files
    )
    absent = tuple(
        ScannedPath(path, intake.revision_kind, PathFreshness.SOURCE_ABSENT, None)
        for path, intake in sorted(intaken.items(), key=lambda item: item[0].value)
        if path not in carried
    )
    return served + absent


def _freshness(
    kind: RevisionKind, published: PublishedRevisionHash, intake: SourceIntake | None
) -> PathFreshness:
    """In sync only when the path last delivered these bytes as this same kind.

    A published revision is its bytes under one kind, so the same bytes last
    taken in as another kind are not yet in the catalog as what the source
    now says they are.
    """

    if intake is None or (intake.revision_kind, intake.revision_hash) != (
        kind,
        published,
    ):
        return PathFreshness.SOURCE_AHEAD
    return PathFreshness.IN_SYNC
