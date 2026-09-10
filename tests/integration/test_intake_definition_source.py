"""What one intake takes into the catalog, and what it refuses before writing.

The durable side is a fake here on purpose: what this layer decides is *which*
files are handed over, under which name, and which of them never reach the
store at all. Whether the handover is one transaction, and whether an already
admitted revision leaves the intake table alone, is the store's own sentence and
is proved against a real store.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from atelier2.adapters.yaml_workflows import parse_workflow_document
from atelier2.application.intake_definition_source import (
    DefinitionSourceIntaken,
    IntakeDefinitionSourceResult,
    IntakeNameUnusable,
    SourcePositionMoved,
    intake_definition_source,
)
from atelier2.application.publish_workflow_revision import WorkflowPublicationLimits
from atelier2.application.scan_definition_source import (
    DefinitionSourceUnknown,
    ScannedDocumentInvalid,
    ScanRefused,
)
from atelier2.contracts.catalog_v3 import (
    CatalogActivatedAt,
    CatalogActor,
    CatalogAdmissionNameHeld,
    CatalogLineageDisplayName,
    CatalogLineageId,
)
from atelier2.contracts.definition_sources import (
    DefinitionSourceAccess,
    DefinitionSourceActor,
    DefinitionSourceConfiguration,
    DefinitionSourceId,
    DefinitionSourceKind,
    DefinitionSourceRefusal,
    DefinitionSourceRevision,
    DefinitionSourceSelection,
    RepositoryLocation,
    RepositoryPath,
    RepositoryRef,
    SelectionPattern,
    SourceCommit,
    SourceIntake,
)
from atelier2.contracts.revisions_v3 import PublishedRevisionHash, RevisionKind
from atelier2.contracts.runs import WorkflowRevision
from atelier2.ports.definition_sources import (
    DefinitionSourceFound,
    DefinitionSourceMissing,
    DefinitionSourceUnreadable,
    PathAlreadyInCatalog,
    PathIntaken,
    ReadDefinitionSourceResult,
    ReadSourceIntakesResult,
    RecordSourceIntakesResult,
    RegisterDefinitionSourceResult,
    ScannedSource,
    SelectedFile,
    SelectedIntake,
    SelectedWorkflow,
    SourceIntakeRecorded,
    SourceIntakeRefused,
)
from tests.scenarios.workflows import declared_output

COMMIT = SourceCommit("c" * 40)
OTHER_COMMIT = SourceCommit("d" * 40)
BUILD = RepositoryPath("workflows/build.yaml")
SHIP = RepositoryPath("workflows/ship.yaml")
ACTOR = CatalogActor("felix")
INTAKEN_AT = CatalogActivatedAt("2026-09-03T08:00:00Z")
LIMITS = WorkflowPublicationLimits(
    maximum_document_bytes=1_000_000,
    maximum_nodes=64,
    maximum_string_characters=4_096,
    maximum_payload_bytes=1_000_000,
)


def workflow_document(name: str) -> bytes:
    return (
        f"""format_version: 3
name: {name}
nodes:
  - id: only
    type: agent
    role: builder
    mode: headless
    instruction: Do the one thing this workflow is for.
""".encode()
        + declared_output()
    )


def registration() -> DefinitionSourceConfiguration:
    return DefinitionSourceConfiguration(
        DefinitionSourceKind.GIT,
        RepositoryLocation("/srv/definitions.git"),
        RepositoryRef("refs/heads/main"),
        DefinitionSourceAccess.ANONYMOUS,
        DefinitionSourceActor("felix"),
        (
            DefinitionSourceSelection(
                SelectionPattern("workflows/*.yaml"), RevisionKind.WORKFLOW
            ),
        ),
    )


@dataclass
class SourcesKeptInMemory:
    """A registrar that answers one prepared outcome and keeps what it was handed.

    What it was handed is the whole assertion: a refusal this layer owns must
    leave this list empty, because an empty list is what "nothing was written"
    looks like from outside the store.
    """

    answer: RecordSourceIntakesResult
    intaken: dict[RepositoryPath, SourceIntake] = field(default_factory=dict)
    handed: list[tuple[SourceCommit, tuple[SelectedIntake, ...]]] = field(
        default_factory=list
    )

    def read_source(self, source_id: DefinitionSourceId) -> ReadDefinitionSourceResult:
        if source_id != registration().source_id:
            return DefinitionSourceMissing(source_id)
        return DefinitionSourceFound(DefinitionSourceRevision(registration(), 1))

    def latest_intakes(self, source_id: DefinitionSourceId) -> ReadSourceIntakesResult:
        del source_id
        return dict(self.intaken)

    def register(
        self, configuration: DefinitionSourceConfiguration
    ) -> RegisterDefinitionSourceResult:
        raise AssertionError("an intake never registers a source")

    def record_intakes(
        self,
        source_id: DefinitionSourceId,
        commit: SourceCommit,
        selected: tuple[SelectedIntake, ...],
        actor: CatalogActor,
        intaken_at: CatalogActivatedAt,
    ) -> RecordSourceIntakesResult:
        del source_id, actor, intaken_at
        self.handed.append((commit, selected))
        return self.answer


@dataclass
class SourceReadingOnce:
    """A reader answering one prepared commit and its files, or one refusal."""

    files: Mapping[RepositoryPath, bytes] = field(default_factory=dict)
    commit: SourceCommit = COMMIT
    refusal: DefinitionSourceRefusal | None = None

    def resolve(self, configuration: DefinitionSourceConfiguration) -> SourceCommit:
        return self.scan(configuration).commit

    def scan(self, configuration: DefinitionSourceConfiguration) -> ScannedSource:
        if self.refusal is not None:
            raise DefinitionSourceUnreadable(self.refusal, "as the scenario prepared")
        return ScannedSource(
            self.commit,
            tuple(
                SelectedFile(path, configuration.selections[0], document)
                for path, document in self.files.items()
            ),
        )


def recorded(*paths: RepositoryPath) -> SourceIntakeRecorded:
    return SourceIntakeRecorded(
        tuple(
            PathIntaken(
                SourceIntake(
                    registration().source_id,
                    path,
                    1,
                    RevisionKind.WORKFLOW,
                    PublishedRevisionHash("0" * 64),
                    COMMIT,
                )
            )
            for path in paths
        )
    )


def intaking(
    sources: SourcesKeptInMemory,
    reader: SourceReadingOnce,
    at_position: SourceCommit | None = None,
) -> IntakeDefinitionSourceResult:
    return intake_definition_source(
        registration().source_id,
        at_position,
        ACTOR,
        INTAKEN_AT,
        sources,
        reader,
        parse_workflow_document,
        LIMITS,
    )


def test_every_selected_file_is_handed_over_once_under_its_authored_name() -> None:
    files = {BUILD: workflow_document("build"), SHIP: workflow_document("ship")}
    sources = SourcesKeptInMemory(recorded(BUILD, SHIP))

    result = intaking(sources, SourceReadingOnce(files))

    assert isinstance(result, DefinitionSourceIntaken)
    assert result.commit == COMMIT
    assert sources.handed == [
        (
            COMMIT,
            (
                SelectedWorkflow(
                    BUILD,
                    WorkflowRevision(files[BUILD]),
                    CatalogLineageDisplayName("build"),
                ),
                SelectedWorkflow(
                    SHIP,
                    WorkflowRevision(files[SHIP]),
                    CatalogLineageDisplayName("ship"),
                ),
            ),
        )
    ]


def test_a_path_the_catalog_already_holds_is_reported_as_already_present() -> None:
    document = workflow_document("build")
    present = PathAlreadyInCatalog(
        BUILD, RevisionKind.WORKFLOW, PublishedRevisionHash.of(document)
    )
    sources = SourcesKeptInMemory(SourceIntakeRecorded((present,)))

    result = intaking(sources, SourceReadingOnce({BUILD: document}))

    assert isinstance(result, DefinitionSourceIntaken)
    assert result.paths == (present,)


def test_a_name_the_catalog_cannot_hold_refuses_before_the_store_is_asked() -> None:
    """A pattern violation is named on its path, and no file of the batch is handed."""

    sources = SourcesKeptInMemory(recorded(BUILD, SHIP))
    reader = SourceReadingOnce(
        {BUILD: workflow_document("build"), SHIP: workflow_document("Ship")}
    )

    result = intaking(sources, reader)

    assert result == IntakeNameUnusable(SHIP, "Ship")
    assert sources.handed == []


def test_a_commit_the_ref_no_longer_stands_at_refuses_before_the_store_is_asked() -> (
    None
):
    sources = SourcesKeptInMemory(recorded(BUILD))
    reader = SourceReadingOnce({BUILD: workflow_document("build")})

    result = intaking(sources, reader, at_position=OTHER_COMMIT)

    assert result == SourcePositionMoved(OTHER_COMMIT, COMMIT)
    assert sources.handed == []


def test_a_document_the_publication_door_refuses_stops_the_intake() -> None:
    sources = SourcesKeptInMemory(recorded(BUILD))
    reader = SourceReadingOnce({BUILD: b"format_version: 3\nname: b\nnodes: []\n"})

    result = intaking(sources, reader)

    assert isinstance(result, ScannedDocumentInvalid)
    assert result.path == BUILD
    assert sources.handed == []


def test_a_source_that_cannot_be_read_is_refused_in_its_own_words() -> None:
    sources = SourcesKeptInMemory(recorded(BUILD))

    result = intaking(
        sources, SourceReadingOnce(refusal=DefinitionSourceRefusal.REF_UNRESOLVED)
    )

    assert result == ScanRefused(
        DefinitionSourceRefusal.REF_UNRESOLVED, "as the scenario prepared"
    )
    assert sources.handed == []


def test_a_source_nobody_registered_is_unknown() -> None:
    unknown = DefinitionSourceId("f" * 64)
    sources = SourcesKeptInMemory(recorded(BUILD))

    result = intake_definition_source(
        unknown,
        None,
        ACTOR,
        INTAKEN_AT,
        sources,
        SourceReadingOnce(),
        parse_workflow_document,
        LIMITS,
    )

    assert result == DefinitionSourceUnknown(unknown)
    assert sources.handed == []


def test_a_path_the_catalog_refuses_is_the_whole_answer() -> None:
    refused = SourceIntakeRefused(
        SHIP,
        CatalogAdmissionNameHeld(
            CatalogLineageDisplayName("ship"), CatalogLineageId("a" * 64)
        ),
    )
    sources = SourcesKeptInMemory(refused)
    reader = SourceReadingOnce(
        {BUILD: workflow_document("build"), SHIP: workflow_document("ship")}
    )

    assert intaking(sources, reader) == refused
